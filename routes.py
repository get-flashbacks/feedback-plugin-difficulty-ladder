"""Difficulty Ladder plugin — backend routes.

Generates a phrase-level difficulty ladder (Easy..Hard) for sloppak
arrangements that don't have one yet — so the frontend's live accuracy
auto-adjust and difficulty HUD (screen.js) have something to work with on songs
that were never authored with phrase data (GP imports, plain single-level
sloppaks).

This module is the plugin's I/O half only: the sloppak dir/zip read+write with
member-name containment checks, Pydantic request bodies, and `setup()`'s route
registration (including the Chordr cross-plugin capability call). The scoring
math it drives lives in the sibling `scoring.py` (Stage 2b, #146/#155), loaded
at setup time through `context["load_sibling"]("scoring")` — the host's
namespaced sibling loader, never at import time.

The seam is the section banner "Pack I/O, request models, and HTTP"
below. `pure-core-has-no-io` enforces the other half's contract: nothing in
`scoring.py` reaches into this module or any I/O module. Cross the seam only
through the `scoring` module object threaded into the callers that need it.

Module map:

  routes.py   this file — pack I/O, request models, HTTP routes, setup().
  scoring.py  the pure chart-scoring core: constants, arrangement
              classification, tempo handling, note grouping, fret/span/
              posture/technique/beat/syncopation scoring, tiering, phrase
              windowing and level materialization. Takes plain dicts/lists and
              returns plain dicts/lists.
"""

import json
import os
import threading
import time
import zipfile
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field, StrictBool
import yaml

import sloppak
from dlc_paths import _resolve_dlc_path
from jsonc import parse_jsonc
from safepath import safe_join


# ── Pack I/O, request models, and HTTP ───────────────────────────────────────
#
# SEAM. Everything below this banner does on-disk work (sloppak dir/zip
# read+write) or HTTP and computes no scoring math; above it is only the
# module docstring and imports. The scoring math this file drives lives in
# the sibling `scoring.py`, loaded at setup through
# `context["load_sibling"]("scoring")`. `pure-core-has-no-io` enforces that
# half's contract: scoring.py may only depend on the pure stdlib set
# {re, bisect, math, dataclasses, itertools} and may not reach into this
# module or any I/O module. No scoring math belongs above this banner.

PLUGIN_ID = "difficulty_ladder"
MAX_PROCESSING_SECONDS = 120  # hard cap per /generate-library call to bound CPU/DoS risk

# Provenance marker (issue #183): a namespaced extension key stamped onto every
# arrangement this plugin generates a ladder for, so a later regenerate can tell
# a plugin-generated `phrases` array apart from a hand-authored one. The feedpak
# spec requires readers to ignore unknown extension keys and the arrangement
# schema is `additionalProperties: true`, so this is additive to the pack. An
# arrangement with `phrases` but no valid marker is treated as authored/unknown
# provenance and is never overwritten without the explicit `overwrite_authored`
# confirmation.
GENERATED_MARKER_KEY = "x_difficulty_ladder"
GENERATED_MARKER_VERSION = 1

_ZIP_ROOT = Path("/_dd_root").resolve()


def _is_generated_ladder(arr: dict) -> bool:
    """True when `arr`'s existing `phrases` were written by this plugin's own
    generator, i.e. carry the current provenance marker. An authored ladder —
    or one written before the marker existed — has no marker and is treated as
    unknown provenance."""
    marker = arr.get(GENERATED_MARKER_KEY)
    return isinstance(marker, dict) and marker.get("version") == GENERATED_MARKER_VERSION


def _stamp_generated_marker(arr: dict, n_levels: int) -> None:
    """Record that this plugin generated `arr`'s ladder — the marker that lets
    a later regenerate tell it apart from an authored one."""
    arr[GENERATED_MARKER_KEY] = {
        "version": GENERATED_MARKER_VERSION,
        "levels": n_levels,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _safe_member_name(rel: str) -> str | None:
    """Canonical, containment-checked member name for `rel`, or None if it
    would escape the pack root. `rel` comes from the manifest's own
    arrangement `file` value — trusted for reading (sloppak.read_member_bytes
    already validates it internally) but NOT pre-validated for this write
    path, so it gets the same safe_join-based check the directory-form write
    path already gets. Mirrors sloppak.py's own _zip_member_key normalization
    (NOT str.lstrip, which strips a character set rather than a "./" prefix
    and would treat "../../x" the same as "x")."""
    safe = safe_join(_ZIP_ROOT, rel or "")
    if safe is None or safe == _ZIP_ROOT:
        return None
    return safe.relative_to(_ZIP_ROOT).as_posix()


def _rewrite_zip_member(zip_path: Path, rel: str, new_bytes: bytes) -> None:
    """Replace ONE member's bytes inside a zip, preserving every other member.

    zipfile has no in-place member update, so this rebuilds the archive into
    a sibling temp file and atomically swaps it in.
    """
    rel_norm = _safe_member_name(rel)
    if rel_norm is None:
        raise ValueError(f"unsafe member path {rel!r}")
    tmp_path = zip_path.with_name(zip_path.name + ".dd_tmp")
    try:
        with zipfile.ZipFile(str(zip_path), "r") as zin:
            infos = zin.infolist()
            with zipfile.ZipFile(str(tmp_path), "w", zipfile.ZIP_DEFLATED) as zout:
                written = False
                for item in infos:
                    item_norm = _safe_member_name(item.filename)
                    if item_norm == rel_norm:
                        zout.writestr(item, new_bytes)
                        written = True
                    else:
                        zout.writestr(item, zin.read(item.filename))
                if not written:
                    zout.writestr(rel_norm, new_bytes)
        os.replace(str(tmp_path), str(zip_path))
    finally:
        # Rebuild failed partway (e.g. disk full) — don't leave a stray
        # .dd_tmp file behind; the original archive is untouched either way
        # since os.replace only runs after the rebuild succeeds.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# One lock per resolved pack path so two requests touching the SAME sloppak
# (e.g. a library-wide sweep and a single-song click, or two arrangements of
# one multi-arrangement song) serialize instead of each reading the original
# file, then racing to os.replace — which would silently drop whichever
# write lost the race. FastAPI runs sync `def` routes in a threadpool, so
# without this, concurrent requests really can interleave.
_pack_locks: dict[str, threading.Lock] = {}
_pack_locks_guard = threading.Lock()


def _lock_for_pack(pack_path: Path) -> threading.Lock:
    key = str(pack_path.resolve())
    with _pack_locks_guard:
        lk = _pack_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _pack_locks[key] = lk
        return lk


def _write_member_bytes(pack_path: Path, rel: str, data: bytes) -> None:
    if pack_path.is_dir():
        target = safe_join(pack_path.resolve(), rel)
        if target is None:
            raise ValueError(f"unsafe member path {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".dd_tmp")
        tmp.write_bytes(data)
        os.replace(str(tmp), str(target))
    else:
        _rewrite_zip_member(pack_path, rel, data)


def _load_manifest_and_arrangement(pack_path: Path, arrangement_index: int):
    manifest = sloppak.load_manifest(pack_path)
    entries = manifest.get("arrangements", []) or []
    if not (0 <= arrangement_index < len(entries)):
        raise HTTPException(400, f"arrangement_index {arrangement_index} out of range")
    entry = entries[arrangement_index]
    if not isinstance(entry, dict):
        raise HTTPException(400, "malformed arrangement entry")

    # Drum-part entries (feedpak 1.17.0 "drums as arrangements") point at a
    # `drum_tab` file, never a note/chord `file` — same routing sloppak.py's
    # own load_song() does. They never carry the notes/chords this generator
    # reads, so this is a clean, expected skip, not an error.
    entry_type = str(entry.get("type") or "").strip().lower()
    if entry_type in ("drums", "drum"):
        return None, None, None, "unsupported-instrument-drums"

    rel = str(entry.get("file", "")).strip()
    if not rel:
        raise HTTPException(400, "arrangement has no backing file")
    raw_bytes = sloppak.read_member_bytes(pack_path, rel)
    if raw_bytes is None:
        raise HTTPException(404, f"arrangement file {rel!r} not found in pack")
    text = raw_bytes.decode("utf-8")
    # read_member_bytes gives us bytes with no on-disk path (zip-form has
    # none at all), so jsonc.load_json (which requires a real Path to
    # stat the .jsonc suffix and read_text itself) doesn't apply here —
    # detect .jsonc by the manifest-declared relpath instead.
    arr = parse_jsonc(text) if rel.lower().endswith(".jsonc") else json.loads(text)
    # `arr` is returned exactly as read -- a read-only load, matching this
    # function's pre-existing contract. `entry` is also returned so a
    # caller can resolve the EFFECTIVE tuning (the manifest entry's own
    # `tuning` overrides the embedded arrangement JSON's, mirroring
    # lib/sloppak.py's load_song(): `if "tuning" in entry: arr.tuning =
    # list(entry["tuning"])`) without that override leaking into whatever
    # the caller does with `arr` next -- in particular, _generate_one
    # writes `arr` straight back into the pack, and the override must
    # never end up persisted into the arrangement file merely because a
    # generation run happened to read it for scoring.
    return rel, arr, entry, None


def _generate_one(pack_path: Path, arrangement_index: int, *, n_levels: int, force: bool, log,
                  scoring, section_times: list[float] | None = None, staged_chords: bool = False,
                  overwrite_authored: bool = False) -> dict:
    # Hold the pack's lock across the whole read-modify-write span. Without
    # this, two requests touching the same pack (a library sweep + a manual
    # click, or two arrangements of one multi-arrangement song) can each read
    # the original zip before either writes, then race to os.replace() —
    # whichever finishes last silently discards the other's phrases.
    with _lock_for_pack(pack_path):
        rel, arr, entry, skip_reason = _load_manifest_and_arrangement(pack_path, arrangement_index)
        if skip_reason:
            instrument = "drums" if skip_reason == "unsupported-instrument-drums" else None
            response = {"ok": True, "skipped": skip_reason, "arrangement_index": arrangement_index}
            if instrument:
                response["instrument"] = instrument
            return response
        instrument = scoring._instrument_kind(arr.get("type", ""), arr.get("name", ""))
        # Usually drums are identified from the manifest entry before we read
        # this file.  Keep this second check because older/malformed packs can
        # label the manifest entry as Lead while the arrangement itself says
        # Drums.  Drum data has different semantics and must never be passed
        # through the fretted/keys phrase generator.
        if instrument == "drums":
            return {
                "ok": True, "skipped": "unsupported-instrument-drums",
                "arrangement_index": arrangement_index, "instrument": instrument,
            }
        if instrument == "unsupported":
            # Explicit allowlist miss (issue #66): a non-empty `type` this
            # generator doesn't recognize (e.g. vocals/harmony/notation) —
            # reported distinctly from "not enough content" so a caller can
            # tell "this generator doesn't support this instrument" apart
            # from "this arrangement was just too short to bother with".
            return {
                "ok": True, "skipped": "unsupported-instrument-type",
                "arrangement_index": arrangement_index, "instrument": instrument,
            }
        if not force and arr.get("phrases"):
            return {
                "ok": True, "skipped": "already-has-phrases",
                "arrangement_index": arrangement_index, "instrument": instrument,
            }
        # #183: `force` replaces an existing ladder, but it must never silently
        # clobber a ladder this plugin did not generate. An arrangement with
        # `phrases` and no (valid) provenance marker is treated as authored or
        # unknown — it needs the explicit `overwrite_authored` confirmation,
        # otherwise this arrangement is reported back unwritten.
        if force and arr.get("phrases") and not overwrite_authored and not _is_generated_ladder(arr):
            return {
                "ok": True, "skipped": "authored-ladder-needs-confirmation",
                "arrangement_index": arrangement_index, "instrument": instrument,
                "needs_confirmation": True,
            }

        # Score against the EFFECTIVE tuning/name/type (manifest entry
        # override, when present) on a shallow copy, so the override --
        # read purely for scoring -- never ends up written back into the
        # pack below via `arr["phrases"] = phrases`. The arrangement
        # file's own fields are left exactly as authored. A malformed
        # manifest `tuning` (not a list -- e.g. an int or null) is
        # ignored rather than raising: `list(...)` on a non-list would
        # otherwise surface as an uncaught 500 well past this route's
        # normal error handling.
        scoring_arr = arr
        effective_tuning = entry.get("tuning") if entry else None
        if isinstance(effective_tuning, list):
            scoring_arr = dict(arr, tuning=list(effective_tuning))
        effective_type = (entry.get("type") if entry else None) or arr.get("type", "")
        effective_name = (entry.get("name") if entry else None) or arr.get("name", "")
        is_bass = scoring._is_bass_arrangement(effective_type, effective_name)
        phrases = scoring.generate_phrases_for_arrangement(
            scoring_arr, n_levels=n_levels, section_times=section_times, is_bass=is_bass,
            staged_chords=staged_chords,
        )
        if phrases is None:
            return {
                "ok": True, "skipped": "not-enough-content-or-unsupported-instrument",
                "arrangement_index": arrangement_index, "instrument": instrument,
            }

        arr["phrases"] = phrases
        _stamp_generated_marker(arr, n_levels)
        new_bytes = json.dumps(arr, ensure_ascii=False).encode("utf-8")
        _write_member_bytes(pack_path, rel, new_bytes)
    log.info("difficulty_ladder: generated %d phrases for %s arrangement %d",
              len(phrases), pack_path.name, arrangement_index)
    return {
        "ok": True, "arrangement_index": arrangement_index,
        "phrases": len(phrases),
        # `requested_levels` is the tier scale the caller asked for
        # (n_levels); `max_difficulty` is the highest phrase max_difficulty
        # actually written -- n_levels - 1 when at least one phrase has a
        # real ladder, 0 when every phrase collapsed to a single level
        # (_collapse_identical_levels). Reporting only `n_levels - 1` here
        # previously claimed a ladder generation may not have produced.
        "requested_levels": n_levels,
        "max_difficulty": max((p["max_difficulty"] for p in phrases), default=0),
        "instrument": instrument,
    }


def _read_pack_json(pack_path: Path, rel: str):
    """Read a JSON/JSONC member from either a directory or zip feedpak."""
    raw = sloppak.read_member_bytes(pack_path, rel)
    if raw is None:
        return None
    text = raw.decode("utf-8")
    return parse_jsonc(text) if rel.lower().endswith(".jsonc") else json.loads(text)


def _canonical_section_times(pack_path: Path, manifest: dict) -> list[float]:
    """Match feedBack's song-level section source for generated phrases.

    feedBack gives a valid ``song_timeline`` precedence over arrangement
    metadata; otherwise it takes sections from the first arrangement that has
    them.  This mirrors that selection so generated phrase intervals line up
    exactly with Section Map's ``highway.getSections()`` boundaries.
    """
    timeline_rel = manifest.get("song_timeline")
    if isinstance(timeline_rel, str) and timeline_rel.strip():
        try:
            timeline = _read_pack_json(pack_path, timeline_rel.strip())
        except ValueError:
            timeline = None
        if isinstance(timeline, dict) and isinstance(timeline.get("beats"), list) and isinstance(timeline.get("sections"), list):
            sections = timeline["sections"]
            times = []
            for section in sections:
                if not isinstance(section, dict):
                    continue
                try:
                    times.append(float(section.get("time", section.get("start_time", 0))))
                except (TypeError, ValueError):
                    continue
            if times:
                return times

    for entry in manifest.get("arrangements", []) or []:
        if not isinstance(entry, dict):
            continue
        rel = str(entry.get("file") or "").strip()
        if not rel:
            continue
        try:
            arrangement = _read_pack_json(pack_path, rel)
        except ValueError:
            continue
        sections = arrangement.get("sections", []) if isinstance(arrangement, dict) else []
        if not isinstance(sections, list) or not sections:
            continue
        times = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            try:
                times.append(float(section.get("time", section.get("start_time", 0))))
            except (TypeError, ValueError):
                continue
        if times:
            return times
    return []


def _generate_song(pack_path: Path, *, n_levels: int, force: bool, log,
                    scoring, staged_chords: bool = False, overwrite_authored: bool = False) -> dict:
    """Generate every eligible arrangement in one song.

    Arrangement indices are manifest/storage indices, not the player UI's
    sorted display positions.  Each arrangement is classified independently
    by ``_generate_one`` so mixed guitar/bass/keys packs work correctly and
    drums (or another unsupported instrument type — issue #66) are
    explicitly reported as skipped, broken out from ``skipped`` into their
    own ``unsupported`` count.

    With ``force`` and no ``overwrite_authored``, an arrangement whose
    existing ladder lacks this plugin's provenance marker (#183) is reported
    under ``needs_confirmation`` and left unwritten.
    """
    manifest = sloppak.load_manifest(pack_path)
    entries = manifest.get("arrangements", []) or []
    section_times = _canonical_section_times(pack_path, manifest)
    results = []
    generated = skipped = failed = unsupported = needs_confirmation = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            results.append({"arrangement_index": index, "skipped": "malformed-arrangement"})
            skipped += 1
            continue
        try:
            result = _generate_one(
                pack_path, index, n_levels=n_levels, force=force, log=log, scoring=scoring,
                section_times=section_times or None, staged_chords=staged_chords,
                overwrite_authored=overwrite_authored,
            )
        except HTTPException as exc:
            # A bad arrangement must not prevent the remaining arrangements
            # in the song from receiving their difficulty ladders.
            result = {"arrangement_index": index, "error": exc.detail}
        except Exception as exc:  # noqa: BLE001 — same best-effort contract as
            # HTTPException above: a JSON/Unicode/filesystem/scoring failure on
            # one arrangement must not abort the rest of the song, and must not
            # bubble up to /generate as an opaque 500 that hides which
            # arrangements did succeed.
            if log is not None:
                log.exception(
                    "difficulty_ladder: unexpected error generating arrangement %d of %s",
                    index, pack_path.name,
                )
            result = {"arrangement_index": index, "error": str(exc)}
        results.append(result)
        if result.get("needs_confirmation"):
            needs_confirmation += 1
        if result.get("skipped") or result.get("error"):
            skipped += 1
            if result.get("error"):
                failed += 1
            elif scoring._is_unsupported_skip(result.get("skipped")):
                unsupported += 1
        else:
            generated += 1
    return {
        "ok": True, "generated": generated, "skipped": skipped,
        "unsupported": unsupported, "failed": failed,
        "needs_confirmation": needs_confirmation, "arrangements": results,
    }


# ── Request models ───────────────────────────────────────────────────────────

class GenerateIn(BaseModel):
    """Body for POST .../generate. `force` is strict (only a real JSON
    boolean, never a truthy-string like "false") since `bool("false")`
    is True in Python and previously let any nonempty string clobber an
    authored ladder; `levels` is bounds-checked here instead of via a
    silent `max(2, min(..., 8))` clamp so out-of-range/non-numeric input
    is rejected (422) rather than silently coerced."""
    filename: str
    levels: int = Field(default=4, ge=2, le=8)
    force: StrictBool = False
    # #183: the explicit second confirmation required to overwrite an existing
    # ladder that carries no provenance marker (authored/unknown). A bare
    # `force: true` regenerates only ladders this plugin generated.
    overwrite_authored: StrictBool = False
    # #103/B10, opt-in: drops a repeated occurrence of an already-Chordr-
    # identified chord at the bottom tier, keeping only its longest-
    # sustained ("landmark") occurrence per phrase. False (the default)
    # reproduces generation output exactly as before this option existed.
    staged_chords: StrictBool = False


class AnalyzeChordsIn(BaseModel):
    """Read-only chord grouping preview for one arrangement."""
    filename: str
    arrangement_index: int = Field(ge=0)


class GenerateLibraryIn(BaseModel):
    """Body for POST .../generate-library — same strictness as GenerateIn,
    plus a bounds-checked max_songs and a bounds-checked processing-time
    cap (issue #40) so a caller can shrink the budget for a quick sweep
    without being able to raise it past MAX_PROCESSING_SECONDS' own
    600s ceiling."""
    levels: int = Field(default=4, ge=2, le=8)
    force: StrictBool = False
    # #183: same authored-ladder guard as GenerateIn — a force sweep does not
    # clobber an unmarked ladder unless this is explicitly set.
    overwrite_authored: StrictBool = False
    max_songs: int = Field(default=500, ge=1, le=2000)
    max_processing_seconds: int = Field(default=MAX_PROCESSING_SECONDS, ge=1, le=600)
    staged_chords: StrictBool = False  # #103/B10, opt-in — see GenerateIn


# ── Routes + Chordr capability call ──────────────────────────────────────────
#
# setup() registers every HTTP route and makes the Chordr cross-plugin
# capability call; the cross-language instrument classification it depends on
# lives in screen.js (see the _instrument_kind/_instrumentKind pairing).

def setup(app, context):
    log = context["log"]
    get_dlc_dir = context["get_dlc_dir"]
    # The pure chart-scoring core now lives in a sibling module, loaded through
    # the host's namespaced loader (never at import time). Threaded into the
    # callers below rather than bound as a module global: the tests call
    # `_generate_one`/`_generate_song` directly without running setup().
    scoring = context["load_sibling"]("scoring")

    def _resolve_pack(dlc_root: Path, filename: str) -> Path:
        safe = _resolve_dlc_path(dlc_root, filename)
        if safe is None:
            raise HTTPException(400, "invalid filename")
        if not sloppak.is_sloppak(safe):
            raise HTTPException(400, "Difficulty Ladder only generates phrase ladders for sloppak/feedpak songs")
        if not safe.exists():
            raise HTTPException(404, "song not found")
        return safe

    @app.post(f"/api/plugins/{PLUGIN_ID}/analyze-chords")
    def analyze_chords(body: AnalyzeChordsIn):
        """Preview Chordr identities and parent groups; never write a pack."""
        dlc_root = get_dlc_dir()
        if dlc_root is None:
            raise HTTPException(400, "no DLC library configured")
        pack_path = _resolve_pack(Path(dlc_root), body.filename.strip())
        try:
            _, arr, entry, skip_reason = _load_manifest_and_arrangement(
                pack_path, body.arrangement_index
            )
        except HTTPException:
            raise
        except FileNotFoundError as exc:
            raise HTTPException(404, "song manifest not found") from exc
        except (UnicodeError, ValueError, yaml.YAMLError, zipfile.BadZipFile) as exc:
            raise HTTPException(400, "malformed arrangement") from exc
        if skip_reason or not isinstance(arr, dict):
            raise HTTPException(400, skip_reason or "malformed arrangement")
        if scoring._instrument_kind(arr.get("type", ""), arr.get("name", "")) != "fretted":
            raise HTTPException(400, "chord grouping requires a fretted arrangement")
        chords = arr.get("chords", [])
        # Resolve the EFFECTIVE tuning (manifest entry override, when
        # present) here too, same as generation -- this endpoint is
        # read-only (never writes the pack), so applying it directly is
        # safe; it just needs to match what playback actually resolves to.
        # A malformed manifest `tuning` (not a list) falls through to the
        # embedded value instead of raising -- the isinstance check below
        # would otherwise never get a chance to catch it as a 400: a bare
        # `list(entry["tuning"])` on a non-list raises TypeError, an
        # uncaught 500, before that check ever runs.
        manifest_tuning = entry.get("tuning") if entry else None
        tuning = list(manifest_tuning) if isinstance(manifest_tuning, list) else arr.get("tuning", [])
        templates = arr.get("templates") or arr.get("chordTemplates") or []
        if not all(isinstance(value, list) for value in (chords, tuning, templates)):
            raise HTTPException(400, "malformed arrangement")
        analyze = getattr(app.state, "chordr_analyze_chart_chords_v1", None)
        if not callable(analyze):
            raise HTTPException(503, "Chordr server analysis is not active")
        # Same EFFECTIVE name/type resolution as generation's is_bass
        # (manifest entry first, embedded fallback) -- a manifest entry
        # authored as a bass part must still select Chordr's bass
        # base-string row even when the embedded arrangement's own
        # type/name doesn't say so. Now uses _is_bass_arrangement (core's
        # real substring semantics) for consistency with generation's
        # is_bass, instead of this endpoint's previous narrower
        # \bbass\b word-boundary match -- see the updated
        # test_chord_preview_does_not_infer_bass_from_name_fragment for
        # the resulting "Ambassador" behavior change (matches core, not
        # a new bug: core's own arrangement_is_bass() has always used a
        # bare substring, so this was already surprising there).
        effective_type = (entry.get("type") if entry else None) or arr.get("type", "")
        effective_name = (entry.get("name") if entry else None) or arr.get("name", "")
        analysis_context = {
            "tuning": tuning,
            "capo": arr.get("capo", 0) or 0,
            "stringCount": len(tuning) or 6,
            "isBass": scoring._is_bass_arrangement(effective_type, effective_name),
        }
        try:
            analysis = analyze(
                chords, context=analysis_context,
                templates=templates,
            )
        except Exception as exc:
            log.exception("difficulty_ladder: Chordr analysis failed for %s", pack_path.name)
            raise HTTPException(503, "Chordr analysis failed") from exc
        return {"ok": True, "filename": body.filename,
                "arrangement_index": body.arrangement_index,
                "chord_count": len(chords), **analysis}

    @app.post(f"/api/plugins/{PLUGIN_ID}/generate")
    def generate(body: GenerateIn):
        filename = body.filename.strip()
        if not filename:
            raise HTTPException(400, "filename required")
        n_levels = body.levels
        force = body.force
        staged_chords = body.staged_chords
        overwrite_authored = body.overwrite_authored

        dlc_root = get_dlc_dir()
        if dlc_root is None:
            raise HTTPException(400, "no DLC library configured")
        pack_path = _resolve_pack(Path(dlc_root), filename)

        try:
            return _generate_song(
                pack_path, n_levels=n_levels, force=force, log=log, scoring=scoring,
                staged_chords=staged_chords, overwrite_authored=overwrite_authored,
            )
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — surface as a clean 500, never crash the server
            log.exception("difficulty_ladder: generate failed for %r", filename)
            raise HTTPException(500, str(e))

    @app.post(f"/api/plugins/{PLUGIN_ID}/generate-library")
    def generate_library(body: GenerateLibraryIn):
        """Best-effort sweep: generate a phrase ladder for every sloppak
        arrangement in the library that doesn't already have one. One bad
        pack must never abort the whole sweep."""
        n_levels = body.levels
        force = body.force
        max_songs = body.max_songs
        max_processing_seconds = body.max_processing_seconds
        staged_chords = body.staged_chords
        overwrite_authored = body.overwrite_authored

        dlc_root = get_dlc_dir()
        if dlc_root is None:
            raise HTTPException(400, "no DLC library configured")
        root = Path(dlc_root)

        generated, skipped, unsupported, failed = 0, 0, 0, []
        needs_confirmation = 0
        scanned = 0
        time_limit_reached = False
        start_time = time.monotonic()
        # Recursive, matching feedBack's own library scanner (lib/scan.py:
        # `dlc.rglob(f"*{ext}")` across both SONG_EXTS) — a shallow
        # root.iterdir() would silently miss any song organized in a
        # subfolder (e.g. DLC/ArtistName/Song.feedpak), which is a layout
        # core's own scan explicitly supports. Sorted (not just deduped via
        # the set) so which packs land inside vs. outside the max_songs/
        # time budget cutoff is deterministic, not filesystem-order-dependent.
        candidates = sorted(
            {p for ext in sloppak.SONG_EXTS for p in root.rglob(f"*{ext}")},
            key=lambda p: p.relative_to(root).as_posix(),
        )
        for entry in candidates:
            if scanned >= max_songs:
                break
            if time.monotonic() - start_time > max_processing_seconds:
                time_limit_reached = True
                break
            if not sloppak.is_sloppak(entry):
                continue
            label = entry.relative_to(root).as_posix()
            scanned += 1
            try:
                manifest = sloppak.load_manifest(entry)
            except Exception as e:  # noqa: BLE001
                failed.append({"filename": label, "error": str(e)})
                continue
            # Same canonical song-level section source _generate_song() uses
            # for the single-song /generate path (issue #67) — without this,
            # a library sweep computed phrase boundaries from each
            # arrangement's own `sections` field instead, which can diverge
            # both from the /generate path and from Section Map's
            # highway.getSections() for the same song.
            try:
                section_times = _canonical_section_times(entry, manifest)
            except Exception as e:  # noqa: BLE001 — a corrupt archive/filesystem
                # error here must not abort songs not yet visited, same
                # best-effort contract as the load_manifest guard above.
                failed.append({"filename": label, "error": str(e)})
                continue
            arr_entries = manifest.get("arrangements", []) or []
            for idx, arr_entry in enumerate(arr_entries):
                if not isinstance(arr_entry, dict) or not str(arr_entry.get("file", "")).strip():
                    continue
                if time.monotonic() - start_time > max_processing_seconds:
                    time_limit_reached = True
                    break
                try:
                    result = _generate_one(
                        entry, idx, n_levels=n_levels, force=force, log=log, scoring=scoring,
                        section_times=section_times or None, staged_chords=staged_chords,
                        overwrite_authored=overwrite_authored,
                    )
                except HTTPException as e:
                    failed.append({"filename": label, "arrangement_index": idx, "error": e.detail})
                    continue
                except Exception as e:  # noqa: BLE001 — keep the sweep going
                    failed.append({"filename": label, "arrangement_index": idx, "error": str(e)})
                    continue
                if result.get("needs_confirmation"):
                    needs_confirmation += 1
                if result.get("skipped"):
                    skipped += 1
                    if scoring._is_unsupported_skip(result.get("skipped")):
                        unsupported += 1
                else:
                    generated += 1
            if time_limit_reached:
                break

        return {
            "ok": True, "scanned": scanned, "generated": generated,
            "skipped": skipped, "unsupported": unsupported, "failed": failed,
            "needs_confirmation": needs_confirmation,
            "time_limit_reached": time_limit_reached,
        }
