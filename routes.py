"""Dynamic Difficulty plugin — backend routes.

Generates a phrase-level difficulty ladder (Easy..Hard) for sloppak
arrangements that don't have one yet, using a note/chord-density heuristic —
so the frontend's live accuracy auto-adjust and glass HUD (screen.js) have
something to work with on songs that were never authored with phrase data
(GP imports, plain single-level sloppaks).

This is a fresh implementation against feedBack's own arrangement wire
format (lib/song.py — t/s/f/sus/... note keys, {t,id,hd,notes:[...]} chords),
operating directly on the raw JSON dicts as stored in a sloppak's
arrangements/*.json. It does not round-trip through the Note/Chord
dataclasses, so any key core doesn't model yet survives untouched, and it
does not port code from Slopsmith's differently-scoped editor plugin —
only the general "score groups, bucket into percentile tiers, thin lower
tiers" approach is reused as a heuristic design, reimplemented here against
feedBack's actual data.
"""

import json
import os
import zipfile
from pathlib import Path

from fastapi import Body, HTTPException

import sloppak
from dlc_paths import _resolve_dlc_path
from jsonc import parse_jsonc
from safepath import safe_join

PLUGIN_ID = "dynamic_difficulty"

MIN_EVENTS_FOR_GENERATION = 8  # skip near-empty arrangements — nothing to grade


# ── Scoring heuristic ────────────────────────────────────────────────────────

def _fret_score(fret):
    if fret <= 0:
        return 0.0
    return min(1.0, fret / 22.0)


def _span_score(notes):
    frets = [n.get("f", 0) for n in notes if n.get("f", 0) > 0]
    if len(frets) < 2:
        return 0.0
    return min(1.0, (max(frets) - min(frets)) / 6.0)


def _tech_score(n):
    score = 0.0
    if n.get("bn"):
        score += 0.4
    if n.get("ho") or n.get("po"):
        score += 0.25
    if n.get("tp"):
        score += 0.5
    if n.get("sl", -1) >= 0 or n.get("slu", -1) >= 0:
        score += 0.2
    if n.get("tr"):
        score += 0.3
    if n.get("hm") or n.get("hp"):
        score += 0.15
    return min(1.0, score)


def _group_notes(notes, chords, *, time_window_ms=150, fret_span_max=4):
    """Group flat wire notes/chords into atomic difficulty-scoring units.

    Simplified relative to a full chart editor's grouping (no link_next
    chain or hand-shape-window arpeggio detection) — explicit chords, then
    time-proximity clusters of otherwise-solo notes (fast runs / implicit
    chord-like clusters), then leftover individual notes.
    """
    groups = []
    for ch in chords:
        groups.append({
            "type": "chord", "notes": list(ch.get("notes", []) or []), "chord": ch,
            "time": float(ch.get("t", 0)), "score": 0.0, "level": 0,
        })

    solo = sorted((dict(n) for n in notes), key=lambda n: float(n.get("t", 0)))
    used = set()
    for i, n in enumerate(solo):
        if i in used:
            continue
        cluster = [n]
        cluster_strings = {n.get("s", 0)}
        frets = [n.get("f", 0)] if n.get("f", 0) > 0 else []
        for j in range(i + 1, len(solo)):
            if j in used:
                continue
            m = solo[j]
            dt_ms = (float(m.get("t", 0)) - float(n.get("t", 0))) * 1000
            if dt_ms > time_window_ms:
                break
            if m.get("s", 0) in cluster_strings:
                continue
            m_fret = m.get("f", 0)
            all_frets = frets + ([m_fret] if m_fret > 0 else [])
            if all_frets and (max(all_frets) - min(all_frets)) > fret_span_max:
                continue
            cluster.append(m)
            cluster_strings.add(m.get("s", 0))
            if m_fret > 0:
                frets.append(m_fret)
            used.add(j)
        used.add(i)
        groups.append({
            "type": "arpeggio" if len(cluster) > 1 else "note",
            "notes": cluster, "chord": None,
            "time": float(cluster[0].get("t", 0)), "score": 0.0, "level": 0,
        })

    groups.sort(key=lambda g: g["time"])
    return groups


def _score_groups(groups, n_strings):
    total = len(groups)
    for gi, g in enumerate(groups):
        ns = g["notes"]
        if not ns:
            g["score"] = 0.0
            continue
        avg_fret = sum(n.get("f", 0) for n in ns) / len(ns)
        fretting = (
            0.4 * _fret_score(avg_fret)
            + 0.35 * _span_score(ns)
            + 0.25 * min(1.0, (len(ns) - 1) / max(n_strings - 1, 1))
        )
        technique = max(_tech_score(n) for n in ns)
        lo = max(0, gi - 5)
        hi = min(total, gi + 6)
        nearby = sum(len(groups[k]["notes"]) for k in range(lo, hi))
        density = min(1.0, nearby / 20.0)
        max_sus = max(float(n.get("sus", 0)) for n in ns)
        sustain_ease = min(1.0, max_sus / 2.0)
        g["score"] = (
            0.35 * fretting + 0.30 * technique + 0.20 * density + 0.15 * (1.0 - sustain_ease)
        )


def _assign_levels(groups, n_levels):
    if not groups:
        return
    scores_sorted = sorted(g["score"] for g in groups)
    total = len(scores_sorted)
    thresholds = [
        scores_sorted[min(int((i + 1) / n_levels * total), total - 1)]
        for i in range(n_levels - 1)
    ]
    for g in groups:
        lvl = 0
        for t in thresholds:
            if g["score"] > t:
                lvl += 1
        g["level"] = min(lvl, n_levels - 1)


def _notes_for_level(groups, level, max_level):
    """Return (notes, chords) wire lists at/below `level`.

    Below the top tier, chords are reduced by voicing and flattened to plain
    notes (no chord_id references — avoids stale chord-template indices on
    a level that never goes through chord reconstruction); the max-difficulty
    tier keeps chords intact and byte-identical to the source.
    """
    out_notes = []
    out_chords = []
    for g in groups:
        if g["level"] > level:
            continue
        if g["type"] == "chord" and g["chord"] is not None:
            if level >= max_level:
                out_chords.append(g["chord"])
                continue
            ch = g["chord"]
            ch_time = float(ch.get("t", 0))
            ch_notes = list(ch.get("notes", []) or [])
            if len(ch_notes) > 1:
                # String-index convention follows the arrangement source
                # (Rocksmith-derived): index 0 = highest-pitched string, so
                # the highest index among a chord's notes is its root.
                ranked = sorted(ch_notes, key=lambda n: n.get("s", 0), reverse=True)
                if level == 0:
                    ch_notes = [ranked[0]]
                elif len(ranked) > 2:
                    ch_notes = ranked[:2]
            for cn in ch_notes:
                merged = dict(cn)
                merged["t"] = ch_time
                out_notes.append(merged)
        elif g["type"] == "arpeggio" and level < max_level:
            ns = g["notes"]
            if level == 0:
                out_notes.append(ns[0])
            else:
                keep_n = max(1, (len(ns) * (level + 1)) // max_level)
                out_notes.extend(ns[:keep_n])
        else:
            out_notes.extend(g["notes"])
    out_notes.sort(key=lambda n: float(n.get("t", 0)))
    out_chords.sort(key=lambda c: float(c.get("t", 0)))
    return out_notes, out_chords


def _generate_anchors(notes, beat_times, *, default_width=4):
    if not notes:
        return []
    anchors = []
    prev_fret = prev_width = None
    for i, bt in enumerate(beat_times):
        bt_end = beat_times[i + 1] if i + 1 < len(beat_times) else bt + 2.0
        window = [n for n in notes if bt <= float(n.get("t", 0)) < bt_end and n.get("f", 0) >= 1]
        if not window:
            continue
        frets = [n.get("f", 0) for n in window]
        min_fret = max(1, min(frets))
        max_fret = max(frets)
        width = max(default_width, max_fret - min_fret + 3)
        if min_fret != prev_fret or width != prev_width:
            anchors.append({"time": round(bt, 3), "fret": min_fret, "width": width})
            prev_fret, prev_width = min_fret, width
    return anchors


def generate_phrases_for_arrangement(arr, *, n_levels=4):
    """Build a phrase-level difficulty ladder for one arrangement's raw wire
    dict (as stored in a sloppak's arrangements/*.json).

    Read-only over `arr` — returns the `phrases` wire list to assign onto
    `arr["phrases"]`; every other key in `arr` is left untouched by the
    caller. Returns None when there isn't enough chart content to bother
    (an ambient/silent arrangement, or one already effectively empty).
    """
    notes = arr.get("notes", []) or []
    chords = arr.get("chords", []) or []
    beats = arr.get("beats", []) or []
    sections = arr.get("sections", []) or []
    tuning = arr.get("tuning", [0] * 6) or [0] * 6
    n_strings = max(1, len(tuning))

    total_events = len(notes) + sum(len(c.get("notes", []) or []) for c in chords)
    if total_events < MIN_EVENTS_FOR_GENERATION:
        return None

    duration = 0.0
    for n in notes:
        duration = max(duration, float(n.get("t", 0)) + float(n.get("sus", 0)))
    for c in chords:
        duration = max(duration, float(c.get("t", 0)) + 0.1)
    if duration <= 0.0:
        duration = 30.0

    if sections:
        secs = sorted(sections, key=lambda s: float(s.get("time", s.get("start_time", 0))))
        windows = []
        for i, s in enumerate(secs):
            t0 = float(s.get("time", s.get("start_time", 0)))
            t1 = (
                float(secs[i + 1].get("time", secs[i + 1].get("start_time", 0)))
                if i + 1 < len(secs) else duration
            )
            windows.append((t0, t1))
    else:
        windows, t = [], 0.0
        while t < duration:
            windows.append((t, min(t + 30.0, duration)))
            t += 30.0
    if not windows:
        windows = [(0.0, duration)]

    groups_all = _group_notes(notes, chords)
    _score_groups(groups_all, n_strings)
    beat_times = [float(b.get("time", 0)) for b in beats]
    max_level = n_levels - 1

    phrases_out = []
    for t0, t1 in windows:
        phrase_groups = [g for g in groups_all if t0 <= g["time"] < t1]
        if not phrase_groups:
            continue
        _assign_levels(phrase_groups, n_levels)
        levels_out = []
        for lvl in range(n_levels):
            lvl_notes, lvl_chords = _notes_for_level(phrase_groups, lvl, max_level)
            lvl_anchors = _generate_anchors(lvl_notes, beat_times)
            levels_out.append({
                "difficulty": lvl,
                "notes": lvl_notes,
                "chords": lvl_chords,
                "anchors": lvl_anchors,
                "handshapes": [],  # not generated in v1 — additive, safe to omit
            })
        phrases_out.append({
            "start_time": round(t0, 3),
            "end_time": round(t1, 3),
            "max_difficulty": max_level,
            "levels": levels_out,
        })
    return phrases_out if phrases_out else None


# ── Sloppak read/write (dir or zip form) ─────────────────────────────────────

def _rewrite_zip_member(zip_path: Path, rel: str, new_bytes: bytes) -> None:
    """Replace ONE member's bytes inside a zip, preserving every other member.

    zipfile has no in-place member update, so this rebuilds the archive into
    a sibling temp file and atomically swaps it in.
    """
    tmp_path = zip_path.with_name(zip_path.name + ".dd_tmp")
    rel_norm = rel.replace("\\", "/").lstrip("./")
    with zipfile.ZipFile(str(zip_path), "r") as zin:
        infos = zin.infolist()
        with zipfile.ZipFile(str(tmp_path), "w", zipfile.ZIP_DEFLATED) as zout:
            written = False
            for item in infos:
                item_norm = item.filename.replace("\\", "/").lstrip("./")
                if item_norm == rel_norm:
                    zout.writestr(item, new_bytes)
                    written = True
                else:
                    zout.writestr(item, zin.read(item.filename))
            if not written:
                zout.writestr(rel, new_bytes)
    os.replace(str(tmp_path), str(zip_path))


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
    rel = str(entry.get("file", "")).strip()
    if not rel:
        raise HTTPException(400, "arrangement has no backing file (drums/pointer entry?)")
    raw_bytes = sloppak.read_member_bytes(pack_path, rel)
    if raw_bytes is None:
        raise HTTPException(404, f"arrangement file {rel!r} not found in pack")
    text = raw_bytes.decode("utf-8")
    # read_member_bytes gives us bytes with no on-disk path (zip-form has
    # none at all), so jsonc.load_json (which requires a real Path to
    # stat the .jsonc suffix and read_text itself) doesn't apply here —
    # detect .jsonc by the manifest-declared relpath instead.
    arr = parse_jsonc(text) if rel.lower().endswith(".jsonc") else json.loads(text)
    return rel, arr


def _generate_one(pack_path: Path, arrangement_index: int, *, n_levels: int, force: bool, log) -> dict:
    rel, arr = _load_manifest_and_arrangement(pack_path, arrangement_index)
    if not force and arr.get("phrases"):
        return {"ok": True, "skipped": "already-has-phrases", "arrangement_index": arrangement_index}

    phrases = generate_phrases_for_arrangement(arr, n_levels=n_levels)
    if phrases is None:
        return {"ok": True, "skipped": "not-enough-content", "arrangement_index": arrangement_index}

    arr["phrases"] = phrases
    new_bytes = json.dumps(arr, ensure_ascii=False).encode("utf-8")
    _write_member_bytes(pack_path, rel, new_bytes)
    log.info("dynamic_difficulty: generated %d phrases for %s arrangement %d",
              len(phrases), pack_path.name, arrangement_index)
    return {
        "ok": True, "arrangement_index": arrangement_index,
        "phrases": len(phrases), "max_difficulty": n_levels - 1,
    }


def setup(app, context):
    log = context["log"]
    get_dlc_dir = context["get_dlc_dir"]

    def _resolve_pack(dlc_root: Path, filename: str) -> Path:
        safe = _resolve_dlc_path(dlc_root, filename)
        if safe is None:
            raise HTTPException(400, "invalid filename")
        if not sloppak.is_sloppak(safe):
            raise HTTPException(400, "Dynamic Difficulty only generates phrase ladders for sloppak/feedpak songs")
        if not safe.exists():
            raise HTTPException(404, "song not found")
        return safe

    @app.post(f"/api/plugins/{PLUGIN_ID}/generate")
    def generate(body: dict = Body(...)):
        filename = str((body or {}).get("filename") or "").strip()
        if not filename:
            raise HTTPException(400, "filename required")
        arrangement_index = int((body or {}).get("arrangement_index", 0) or 0)
        n_levels = max(2, min(int((body or {}).get("levels", 4) or 4), 8))
        force = bool((body or {}).get("force", False))

        dlc_root = get_dlc_dir()
        if dlc_root is None:
            raise HTTPException(400, "no DLC library configured")
        pack_path = _resolve_pack(Path(dlc_root), filename)

        try:
            return _generate_one(pack_path, arrangement_index, n_levels=n_levels, force=force, log=log)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — surface as a clean 500, never crash the server
            log.exception("dynamic_difficulty: generate failed for %r", filename)
            raise HTTPException(500, str(e))

    @app.post(f"/api/plugins/{PLUGIN_ID}/generate-library")
    def generate_library(body: dict = Body(...)):
        """Best-effort sweep: generate a phrase ladder for every sloppak
        arrangement in the library that doesn't already have one. One bad
        pack must never abort the whole sweep."""
        n_levels = max(2, min(int((body or {}).get("levels", 4) or 4), 8))
        force = bool((body or {}).get("force", False))
        max_songs = max(1, min(int((body or {}).get("max_songs", 500) or 500), 2000))

        dlc_root = get_dlc_dir()
        if dlc_root is None:
            raise HTTPException(400, "no DLC library configured")
        root = Path(dlc_root)

        generated, skipped, failed = 0, 0, []
        scanned = 0
        for entry in sorted(root.iterdir()):
            if scanned >= max_songs:
                break
            if not sloppak.is_sloppak(entry):
                continue
            scanned += 1
            try:
                manifest = sloppak.load_manifest(entry)
            except Exception as e:  # noqa: BLE001
                failed.append({"filename": entry.name, "error": str(e)})
                continue
            arr_entries = manifest.get("arrangements", []) or []
            for idx, arr_entry in enumerate(arr_entries):
                if not isinstance(arr_entry, dict) or not str(arr_entry.get("file", "")).strip():
                    continue
                try:
                    result = _generate_one(entry, idx, n_levels=n_levels, force=force, log=log)
                except HTTPException as e:
                    failed.append({"filename": entry.name, "arrangement_index": idx, "error": e.detail})
                    continue
                except Exception as e:  # noqa: BLE001 — keep the sweep going
                    failed.append({"filename": entry.name, "arrangement_index": idx, "error": str(e)})
                    continue
                if result.get("skipped"):
                    skipped += 1
                else:
                    generated += 1

        return {"ok": True, "scanned": scanned, "generated": generated, "skipped": skipped, "failed": failed}
