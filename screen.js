(function () {
    'use strict';

    // Re-hydration guard (plugin-runtime-idempotent.v1 / spec §6.1): the Host
    // MAY re-run this script mid-session (e.g. the plugin set reloads). This
    // plugin is a persistent background overlay with no per-visit UI to
    // refresh (unlike a nav screen reacting to screen:changed), so a second
    // execution has nothing useful to do — without this guard it would start
    // a second parallel pair of rAF loops and duplicate every event listener
    // registered below.
    var _singleton = (window.__feedBackDynamicDifficulty = window.__feedBackDynamicDifficulty || { installed: false });
    if (_singleton.installed) return;
    _singleton.installed = true;

    var PLUGIN_ID = 'difficulty_ladder';
    var LS_PREFIX = 'difficulty_ladder.';

    // Section Map's released integration probe predates this plugin's rename
    // from dynamic_difficulty.  It subscribes to our public
    // `difficulty:sections-updated` event only after seeing this capability
    // marker. Keep the compatibility surface deliberately minimal: section
    // boundaries remain the host's canonical highway.getSections() data, and
    // the event payload is indexed against that exact array.
    window._ddCapabilities = window._ddCapabilities || {};
    window._ddCapabilities.sectionDifficulty = true;

    function lsGet(key, def) {
        try {
            var v = localStorage.getItem(LS_PREFIX + key);
            return v === null ? def : JSON.parse(v);
        } catch (_) { return def; }
    }
    function lsSet(key, val) {
        try { localStorage.setItem(LS_PREFIX + key, JSON.stringify(val)); } catch (_) { /* noop */ }
    }
    var _pendingSettingWrites = {};
    function lsSetDebounced(key, val) {
        if (_pendingSettingWrites[key]) clearTimeout(_pendingSettingWrites[key]);
        _pendingSettingWrites[key] = setTimeout(function () {
            delete _pendingSettingWrites[key];
            lsSet(key, val);
        }, 150);
    }
    function cancelDebouncedSettingWrite(key) {
        if (!_pendingSettingWrites[key]) return;
        clearTimeout(_pendingSettingWrites[key]);
        delete _pendingSettingWrites[key];
    }

    // ---- Per-song difficulty memory ----
    // Core only persists master_difficulty as a single global (server.py's
    // /api/settings) — switching songs mid-session keeps whatever % the
    // previous song ended on. This remembers each song's own last-used value
    // (Slopsmith's song_mastery plugin did the same, per-filename) so
    // revisiting a song you'd auto-adjusted or manually set restores where
    // you left off, instead of inheriting an unrelated song's difficulty.
    var SONG_MASTERY_LS_KEY = LS_PREFIX + 'songMastery';
    // Lazily loaded, kept in sync by saveSongMasteryMap() — avoids a fresh
    // JSON.parse of the whole map on every window.setMastery() call, which
    // slider drags can fire many times a second via oninput.
    var _songMasteryMapCache = null;

    function loadSongMasteryMap() {
        if (_songMasteryMapCache) return _songMasteryMapCache;
        try {
            var parsed = JSON.parse(localStorage.getItem(SONG_MASTERY_LS_KEY) || '{}');
            // typeof [] === 'object' too — an array here would make
            // map[_songKey] = pct set a non-index property that
            // JSON.stringify silently drops from array output, so per-song
            // mastery would never actually persist. Reject arrays explicitly.
            _songMasteryMapCache = (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) ? parsed : {};
        } catch (_) {
            _songMasteryMapCache = {};
        }
        return _songMasteryMapCache;
    }
    function saveSongMasteryMap(map) {
        _songMasteryMapCache = map;
        try { localStorage.setItem(SONG_MASTERY_LS_KEY, JSON.stringify(map)); } catch (_) { /* noop */ }
    }
    function _masteryPct(record) {
        var value = record && typeof record === 'object' ? record.mastery : record;
        return (typeof value === 'number' && isFinite(value)) ? value : null;
    }
    function _rememberSongInstrument(key, instrument) {
        if (!key || !instrument) return;
        var map = loadSongMasteryMap();
        var pct = _masteryPct(map[key]);
        if (map[key] && typeof map[key] === 'object' && map[key].instrument === instrument) return;
        map[key] = { mastery: pct, instrument: instrument };
        saveSongMasteryMap(map);
    }

    // ---- Library card badge (issue #4) ----
    // Surfaces the songMastery map above as a library-card decoration via the
    // Host's registration API (window.feedBack.libraryCardActions) — never a
    // MutationObserver on library DOM (CLAUDE.md's performance section calls
    // that anti-pattern out explicitly; this is exactly the extension point
    // the capability exists to replace it with).
    //
    // Known limitation (documented, not silently worked around): the
    // registered action's `label`/`icon` are static strings fixed at
    // register()-time — core's `list(song)` returns the SAME label/icon for
    // every card the predicate applies to, with no per-song text hook in this
    // capability's v1 shape (checked static/capabilities/library-card-actions.js
    // and static/v3/songs.js's songCard() rendering — `label`/`icon` come from
    // the registered spec object itself, not a value computed per `song`).
    // A literal "badge showing this song's exact N%" therefore isn't
    // expressible through `libraryCardActions` as it exists today; this
    // registers an applies()-gated indicator (visible only on cards that HAVE
    // a remembered difficulty) with the exact percentage in its title/aria
    // label, and a generic glyph otherwise — the closest faithful
    // approximation, with the exact-text gap filed as a follow-up.
    function _dominantSongMastery(song) {
        if (!song || !song.filename) return null;
        var map = loadSongMasteryMap();
        var prefix = song.filename + '::';
        var fallback = null;
        for (var k in map) {
            if (!Object.prototype.hasOwnProperty.call(map, k) || k.indexOf(prefix) !== 0) continue;
            var v = _masteryPct(map[k]);
            if (v === null) continue;
            if (k === prefix + '0') return v; // prefer the first/primary arrangement
            if (fallback === null) fallback = v;
        }
        return fallback;
    }

    function registerLibraryCardBadge() {
        var fb = window.feedBack;
        if (!fb || !fb.libraryCardActions || typeof fb.libraryCardActions.register !== 'function') return;
        if (window.__ddCardBadgeRegistered) return; // idempotent — see plugin-runtime-idempotent.v1 guard at top of file
        window.__ddCardBadgeRegistered = true;
        fb.libraryCardActions.register({
            id: 'difficulty_ladder.mastery_badge',
            pluginId: PLUGIN_ID,
            label: 'Last played at a remembered difficulty (see this song\'s card menu for the exact %)',
            icon: '🥃',
            placement: 'overlay',
            order: 90,
            // O(1)/allocation-light per card, per the capability's own contract
            // (list(song) runs this once per visible card on every re-render).
            applies: function (song) { return _dominantSongMastery(song) !== null; },
            // Purely informational — nothing to run. Re-affirms the saved value
            // so a click is harmless rather than surprising.
            run: function (song) {
                var pct = _dominantSongMastery(song);
                return { ok: true, mastery: pct };
            },
        });
    }

    // Wraps the single global entry point every mastery change already flows
    // through — the manual player slider's oninput, the Gameplay-tab speed
    // slider, and this plugin's own auto-adjust all call window.setMastery()
    // (see feedBack's player-controls.js _applyMastery). Wrapping it here,
    // rather than listening for a dedicated "mastery changed" event, catches
    // every source without needing one. Idempotent — checks a marker so a
    // plugin-runtime-idempotent.v1 re-run never double-wraps.
    function ensureMasterySaveHook() {
        if (typeof window.setMastery !== 'function' || window.setMastery.__ddWrapped) return;
        var orig = window.setMastery;
        function wrapped() {
            // apply()/arguments/return-value forwarding: this is a general-
            // purpose wrap of a shared core entry point, not a call site we
            // control, so preserve `this`, every argument, and whatever orig
            // hands back rather than assuming its current single-arg shape.
            var result = orig.apply(this, arguments);
            _onMasteryApplied(arguments[0]);
            return result;
        }
        wrapped.__ddWrapped = true;
        window.setMastery = wrapped;
    }

    function _onMasteryApplied(v) {
        if (!_songKey) return; // no song loaded yet (e.g. core's own settings hydration)
        var hw = window.highway;
        if (!hw || typeof hw.hasPhraseData !== 'function' || !hw.hasPhraseData()) return;
        var pct = parseInt(v, 10);
        if (!isFinite(pct)) return;
        pct = Math.max(0, Math.min(100, pct));
        var map = loadSongMasteryMap();
        // Emit updated section difficulties when mastery changes
        calculateAndEmitSectionDifficulties();
        // Slider drags fire oninput per pixel — window.setMastery() (and thus
        // this hook) can run many times a second. Skip the parse/stringify/
        // write when the stored value hasn't actually changed.
        var current = map[_songKey];
        var instrument = _songInstrument
            || (current && typeof current === 'object' ? current.instrument : null);
        if (_masteryPct(current) === pct
            && (!instrument || (current && current.instrument === instrument))) return;
        map[_songKey] = instrument
            ? { mastery: pct, instrument: instrument }
            : pct;
        saveSongMasteryMap(map);
    }

    // Called once per song change (see onSongEvent). Applies this song's own
    // remembered difficulty, if any, over whatever global value core just
    // carried over from the previous song.
    function _maybeRestoreSongMastery(key) {
        if (!key) return;
        var hw = window.highway;
        if (!hw || typeof hw.hasPhraseData !== 'function' || !hw.hasPhraseData()) return;
        var saved = _masteryPct(loadSongMasteryMap()[key]);
        if (saved === null) return;
        if (typeof window.setMastery === 'function') window.setMastery(saved);
    }

    // ---- Settings (localStorage-backed; see settings.html for the panel) ----
    var settings = {
        autoAdjust: lsGet('autoAdjust', false),
        dropResistance: lsGet('dropResistance', false) === true,
        showGlasses: lsGet('showGlasses', true),
        sensitivity: lsGet('sensitivity', 2),     // 1 (lenient) .. 3 (strict) — confidence thresholds + step size
        reactionSpeed: lsGet('reactionSpeed', 2), // 1 (slow) .. 3 (fast) — EMA_ALPHA, how much one phrase's result moves the rolling average
        minMastery: lsGet('minMastery', 0),     // percent
        maxMastery: lsGet('maxMastery', 100),   // percent
        generateLevels: lsGet('generateLevels', 4), // 2..8 cap — phrase-ladder tier cap sent to /generate
    };

    function thresholds() {
        var s = Math.max(1, Math.min(3, settings.sensitivity));
        return {
            up: 0.93 - (s - 1) * 0.05,     // 1:0.93  2:0.88  3:0.83
            down: 0.65 + (s - 1) * 0.03,   // 1:0.65  2:0.68  3:0.71
            step: 10 + (s - 1) * 5,        // percent step: 10 / 15 / 20
        };
    }

    // reactionSpeed's default (2) resolves to 0.35 — the value EMA_ALPHA was
    // hardcoded to before this setting existed — so a user who never touches
    // the new slider sees byte-identical auto-adjust behavior to before
    // (issue #5's acceptance criterion). 1 (slow) smooths more, weighting a
    // single phrase's result less; 3 (fast) reacts to a run of good/bad
    // sections sooner.
    function emaAlpha() {
        var s = Math.max(1, Math.min(3, settings.reactionSpeed));
        return 0.20 + (s - 1) * 0.15; // 1:0.20  2:0.35  3:0.50
    }

    // Fixed for now (not settings) — kept as named top-level constants, same
    // treatment as thresholds()/emaAlpha() above, so a future settings-slider
    // addition can follow the exact pattern already established for
    // sensitivity/reactionSpeed.
    var WARMUP_PHRASES = 2;  // phrases scored on a fresh song before auto-adjust may act
    var RAMP_PHRASES = 3;    // qualifying phrases a full th.step move is spread over
    var DOWN_CONFIRM_PHRASES = 2;

    function rampStep(th, progress) {
        var index = Math.max(0, Math.min(RAMP_PHRASES - 1, Number(progress) || 0));
        var before = Math.round(th.step * index / RAMP_PHRASES);
        var after = Math.round(th.step * (index + 1) / RAMP_PHRASES);
        return Math.max(1, after - before);
    }

    // ---- Per-song scoring state ----
    var _songKey = null;
    var _songInstrument = null;    // authoritative routes.py classification when available
    var _emaHitRate = null;        // null = no phrase scored yet this song
    // EMA weight is now the reactionSpeed setting (emaAlpha(), above) rather
    // than a hardcoded constant — see issue #5. Read live (not cached) since
    // the settings-changed listener below can update settings.reactionSpeed
    // mid-song.
    var _judgedKeys = null;        // Set, reset every phrase to bound memory
    var _phraseHits = 0;
    var _phraseTotal = 0;
    var _phrasesScored = 0;        // counts real phrases committed this song, gates WARMUP_PHRASES
    var _curPhraseIdx = -1;
    var _lastObservedMasteryPct = null; // detects manual slider changes before or after auto-apply
    var _lastAutoAction = null;     // { direction, pct, reason } - for diagnostics
    var _rampDirection = null;
    var _rampProgress = 0;
    var _downStreak = 0;
    // Forward-advancing cursors into the time-sorted notes/chords arrays —
    // avoids an O(N) full-array rescan every rAF tick (CLAUDE.md's per-frame
    // performance doctrine). Reset only on a backward seek (loop/rewind).
    var _noteCursor = 0;
    var _chordCursor = 0;
    var _lastScoredT = -1;

    function songKeyOf(si) {
        if (!si) return null;
        var arrKey = (si.arrangement_index != null) ? si.arrangement_index : (si.arrangement || '');
        return (si.filename || '') + '::' + arrKey;
    }

    function resetPerSongState() {
        _emaHitRate = null;
        _judgedKeys = new Set();
        _phraseHits = 0;
        _phraseTotal = 0;
        _phrasesScored = 0;
        _curPhraseIdx = -1;
        _lastObservedMasteryPct = null;
        _lastAutoAction = null;
        _rampDirection = null;
        _rampProgress = 0;
        _downStreak = 0;
        _noteCursor = 0;
        _chordCursor = 0;
        _lastScoredT = -1;
    }
    resetPerSongState();

    function judgmentKey(time, s, f) { return time + '_' + s + '_' + f; }

    function commitPhraseResult(ratio) {
        var alpha = emaAlpha();
        _emaHitRate = (_emaHitRate == null) ? ratio : (alpha * ratio + (1 - alpha) * _emaHitRate);
        // Counts every phrase actually played this song, regardless of
        // autoAdjust — a warm-up satisfied while paused should still count
        // once the user flips auto-adjust back on, rather than resetting.
        _phrasesScored++;
        if (!settings.autoAdjust) {
            _rampDirection = null;
            _rampProgress = 0;
            _downStreak = 0;
            contributeDiagnostics();
            return;
        }
        var hw = window.highway;
        if (!hw || typeof hw.getMastery !== 'function') {
            _rampDirection = null;
            _rampProgress = 0;
            _downStreak = 0;
            contributeDiagnostics();
            return;
        }
        // Cold-start guard: don't let a single nervous/rusty first section on
        // a fresh song swing the slider before there's enough signal.
        if (_phrasesScored < WARMUP_PHRASES) {
            contributeDiagnostics();
            return;
        }

        var curPct = Math.round(hw.getMastery() * 100);
        // Manual-override doctrine: a human action always wins over automation.
        // If the live value has drifted from what we last observed, someone
        // moved the slider themselves — stand down instead of fighting them.
        if (_lastObservedMasteryPct != null && curPct !== _lastObservedMasteryPct) {
            _rampDirection = null;
            _rampProgress = 0;
            _downStreak = 0;
            settings.autoAdjust = false;
            lsSet('autoAdjust', false);
            syncControlsUI();
            contributeDiagnostics();
            return;
        }

        var th = thresholds();
        var direction = _emaHitRate >= th.up ? 'up' : _emaHitRate <= th.down ? 'down' : null;
        _downStreak = direction === 'down' && settings.dropResistance ? _downStreak + 1 : 0;
        if (direction == null) {
            _rampDirection = null;
            _rampProgress = 0;
            contributeDiagnostics();
            return;
        }
        _lastObservedMasteryPct = curPct;
        if (direction === 'down' && settings.dropResistance && _downStreak < DOWN_CONFIRM_PHRASES) {
            _rampDirection = null;
            _rampProgress = 0;
            contributeDiagnostics();
            return;
        }
        if (_rampDirection !== direction) {
            _rampDirection = direction;
            _rampProgress = 0;
        }
        var step = rampStep(th, _rampProgress);
        var next = direction === 'up' ? curPct + step : curPct - step;
        next = Math.max(settings.minMastery, Math.min(settings.maxMastery, next));

        if (next !== curPct && typeof window.setMastery === 'function') {
            window.setMastery(next);
            _lastObservedMasteryPct = next;
            _rampProgress = (_rampProgress + 1) % RAMP_PHRASES;
            var dir = next > curPct ? 'up' : 'down';
            _lastAutoAction = {
                direction: dir,
                pct: next,
                step: step,
                reason: dir === 'up' ? 'ema_above_up_threshold' : 'ema_below_down_threshold',
            };
        }
        contributeDiagnostics();
    }

    function contributeDiagnostics() {
        var fb = window.feedBack;
        if (!fb || !fb.diagnostics || typeof fb.diagnostics.contribute !== 'function') return;
        var hw = window.highway;
        var provider = hw && typeof hw.getNoteStateProvider === 'function' ? hw.getNoteStateProvider() : null;
        fb.diagnostics.contribute(PLUGIN_ID, {
            schema: 'difficulty_ladder.v1',
            ema_hit_rate: _emaHitRate,
            last_auto_action: _lastAutoAction,
            provider_registered: !!provider,
            auto_adjust_enabled: settings.autoAdjust,
            show_glasses: settings.showGlasses,
        });
    }

    // Calculate and emit section difficulty data for other plugins (e.g., sectionmap)
    function calculateAndEmitSectionDifficulties() {
        var hw = window.highway;
        var fb = window.feedBack;

        // Early exit if dependencies aren't available
        if (!hw || typeof hw.getSections !== 'function' || !fb || typeof fb.emit !== 'function') return;
        if (typeof hw.getPhrases !== 'function' || !hw.getPhrases()) return;

        var sections = hw.getSections();
        var phrases = hw.getPhrases();
        var mastery = typeof hw.getMastery === 'function' ? hw.getMastery() : 0.5;

        if (!sections || sections.length === 0 || !phrases || phrases.length === 0) return;

        // Calculate max difficulty across all phrases
        var maxDiff = 1;
        for (var i = 0; i < phrases.length; i++) {
            maxDiff = Math.max(maxDiff, phrases[i].max_difficulty);
        }

        // Map sections to difficulty data
        var sectionDifficulties = {};
        for (var si = 0; si < sections.length; si++) {
            var section = sections[si];
            var nextSectionTime = si < sections.length - 1 ? sections[si + 1].time : Infinity;

            // Find phrases within this section's time range
            var sectionDifficultiesInRange = [];
            for (var pi = 0; pi < phrases.length; pi++) {
                var phrase = phrases[pi];
                // Check if phrase overlaps with section
                if (phrase.end_time > section.time && phrase.start_time < nextSectionTime) {
                    sectionDifficultiesInRange.push(phrase.max_difficulty);
                }
            }

            if (sectionDifficultiesInRange.length > 0) {
                // Use the average difficulty in this section
                var avgDifficulty = sectionDifficultiesInRange.reduce(function(a, b) { return a + b; }, 0) / sectionDifficultiesInRange.length;
                var maxSectionDifficulty = Math.max.apply(Math, sectionDifficultiesInRange);

                // Calculate fill percentage based on mastery vs max difficulty
                var fillPercentage = maxDiff > 0 ? Math.min(100, (mastery * maxSectionDifficulty / maxDiff) * 100) : 0;

                // Determine glass size based on section difficulty
                var glassSize = 'medium';
                if (maxSectionDifficulty < maxDiff * 0.33) glassSize = 'small';
                else if (maxSectionDifficulty > maxDiff * 0.66) glassSize = 'large';

                sectionDifficulties[si] = {
                    fillPercentage: fillPercentage,
                    glassSize: glassSize,
                    avgDifficulty: avgDifficulty,
                    maxDifficulty: maxSectionDifficulty,
                };
            }
        }

        // Emit event for sectionmap and other interested plugins
        fb.emit('difficulty:sections-updated', {
            sectionDifficulties: sectionDifficulties,
            mastery: mastery,
            maxDifficulty: maxDiff,
        });
    }

    // Reads live per-note judgments through the note-state provider slot
    // (owned by whichever scorer plugin, e.g. note_detect, is active). This
    // is a read, not a takeover — highway.getNoteStateProvider() is a public
    // getter documented for exactly this kind of consumption.
    var _scoreRafHandle = null;
    function tickScoring() {
        if (!isPlayerActive()) {
            _scoreRafHandle = null;
            return;
        }
        _scoreRafHandle = requestAnimationFrame(tickScoring);

        // Split Screen owns separate highway instances and intentionally
        // suppresses the main-player detector.  Score those instances here,
        // with state isolated per panel, before following the normal main
        // highway path below.
        tickSplitScoring();

        var hw = window.highway;
        if (!hw || typeof hw.hasPhraseData !== 'function' || !hw.hasPhraseData()) return;
        var provider = typeof hw.getNoteStateProvider === 'function' ? hw.getNoteStateProvider() : null;
        if (!provider) return; // no active scorer — nothing to react to yet

        var phrases = hw.getPhrases();
        if (!phrases || phrases.length === 0) return;
        var t = hw.getTime();

        // A backward jump (loop restart, user seek, section-practice rewind)
        // invalidates the forward-only cursors below — resync from scratch.
        // This branch is the only O(N)-ish path here and it's seek-triggered,
        // not per-frame.
        if (t < _lastScoredT - 0.05) {
            _noteCursor = 0;
            _chordCursor = 0;
        }
        _lastScoredT = t;

        var idx = _curPhraseIdx;
        if (idx < 0 || t < phrases[idx].start_time || t >= phrases[idx].end_time) {
            idx = phrases.findIndex(function (p) { return t >= p.start_time && t < p.end_time; });
        }
        if (idx !== _curPhraseIdx) {
            if (_curPhraseIdx >= 0 && _phraseTotal > 0) commitPhraseResult(_phraseHits / _phraseTotal);
            _curPhraseIdx = idx;
            _phraseHits = 0;
            _phraseTotal = 0;
            _judgedKeys = new Set();
        }
        if (idx < 0) return;

        var p = phrases[idx];
        var lookback = 0.6;   // seconds — give the scorer time to settle a judgment
        var windowStart = Math.max(p.start_time, t - 2.0);

        // Notes/chords arrive time-sorted (core guarantee — see highway.js).
        // Advance each cursor past everything older than windowStart ONCE;
        // it never needs to look at that prefix again, so this amortizes to
        // O(total events in the song) instead of O(N) every rAF tick.
        var notes = typeof hw.getFilteredNotes === 'function' ? hw.getFilteredNotes() : [];
        while (_noteCursor < notes.length && notes[_noteCursor].t < windowStart) _noteCursor++;
        for (var i = _noteCursor; i < notes.length; i++) {
            var n = notes[i];
            if (n.t > t - lookback) break;
            if (n.t >= p.end_time) break;
            var nk = judgmentKey(n.t, n.s, n.f);
            if (_judgedKeys.has(nk)) continue;
            var nst = provider(n, n.t);
            if (!nst) continue;
            var nname = typeof nst === 'string' ? nst : nst.state;
            // 'active' (a sustain currently being held correctly) is
            // deliberately NOT counted here — it's an ongoing render signal
            // for the note's glow, not a separate scoring event, and the
            // note's onset is assumed to already resolve to 'hit'/'miss' on
            // its own judgment key elsewhere in the provider's lifecycle.
            // Revisit if that assumption turns out wrong for a given scorer.
            if (nname === 'hit' || nname === 'miss') {
                _judgedKeys.add(nk);
                _phraseTotal++;
                if (nname === 'hit') _phraseHits++;
            }
        }

        var chords = typeof hw.getFilteredChords === 'function' ? hw.getFilteredChords() : [];
        while (_chordCursor < chords.length && chords[_chordCursor].t < windowStart) _chordCursor++;
        for (var j = _chordCursor; j < chords.length; j++) {
            var c = chords[j];
            if (c.t > t - lookback) break;
            if (c.t >= p.end_time) break;
            var cnotes = c.notes || [];
            for (var k = 0; k < cnotes.length; k++) {
                var cn = cnotes[k];
                var ck = judgmentKey(c.t, cn.s, cn.f);
                if (_judgedKeys.has(ck)) continue;
                var cst = provider(cn, c.t);
                if (!cst) continue;
                var cname = typeof cst === 'string' ? cst : cst.state;
                // 'active' excluded here too — same rationale as the note loop above.
                if (cname === 'hit' || cname === 'miss') {
                    _judgedKeys.add(ck);
                    _phraseTotal++;
                    if (cname === 'hit') _phraseHits++;
                }
            }
        }
    }

    // ---- Split Screen adaptation -----------------------------------------
    // Split Screen creates each panel detector through the public
    // createNoteDetector({ highway, ownSource }) factory.  Observing that
    // construction is the only Ladder-side integration needed: it avoids
    // reaching into Split Screen's private panel array, while giving us the
    // exact highway that owns the note-state provider.
    var _splitScoreStates = new Map();
    var _splitPanelsUnsubscribe = null;

    function newSplitScoreState() {
        return {
            judgedKeys: new Set(), phraseHits: 0, phraseTotal: 0,
            phrasesScored: 0, curPhraseIdx: -1, lastScoredT: -1,
            noteCursor: 0, chordCursor: 0, emaHitRate: null,
            lastObservedMasteryPct: null, rampDirection: null,
            rampProgress: 0, downStreak: 0,
        };
    }

    function registerSplitHighway(hw) {
        if (!hw || _splitScoreStates.has(hw)) return;
        _splitScoreStates.set(hw, newSplitScoreState());
        startRafLoops();
    }

    function installSplitScreenDetectorHook() {
        var factory = window.createNoteDetector;
        if (typeof factory !== 'function' || factory.__ddSplitWrapped) return;
        function wrapped(options) {
            var detector = factory.apply(this, arguments);
            var ss = window.feedBackSplitscreen || window.slopsmithSplitscreen;
            if (options && options.ownSource === true && options.highway
                && ss && typeof ss.isActive === 'function' && ss.isActive()) {
                registerSplitHighway(options.highway);
                // Split Screen destroys and recreates detectors when a panel
                // changes arrangement.  Release its isolated score state at
                // the same lifecycle edge so obsolete highways cannot keep
                // a rAF-side reference alive.
                if (detector && typeof detector.destroy === 'function' && !detector.destroy.__ddSplitWrapped) {
                    var destroy = detector.destroy;
                    function wrappedDestroy() {
                        _splitScoreStates.delete(options.highway);
                        return destroy.apply(this, arguments);
                    }
                    wrappedDestroy.__ddSplitWrapped = true;
                    detector.destroy = wrappedDestroy;
                }
            }
            return detector;
        }
        wrapped.__ddSplitWrapped = true;
        window.createNoteDetector = wrapped;
    }

    function startSplitScreenHookSubscription() {
        var fb = window.feedBack;
        if (_splitPanelsUnsubscribe || !fb || typeof fb.on !== 'function') return;
        var handler = installSplitScreenDetectorHook;
        var unsubscribe = fb.on('splitscreen:panels-changed', handler);
        _splitPanelsUnsubscribe = typeof unsubscribe === 'function'
            ? unsubscribe
            : (typeof fb.off === 'function' ? function () { fb.off('splitscreen:panels-changed', handler); } : function () {});
    }

    function stopSplitScreenHookSubscription() {
        if (!_splitPanelsUnsubscribe) return;
        _splitPanelsUnsubscribe();
        _splitPanelsUnsubscribe = null;
    }

    function commitSplitPhraseResult(state, hw, ratio) {
        var alpha = emaAlpha();
        state.emaHitRate = state.emaHitRate == null ? ratio : alpha * ratio + (1 - alpha) * state.emaHitRate;
        state.phrasesScored++;
        if (!settings.autoAdjust || !hw || typeof hw.getMastery !== 'function') {
            state.downStreak = 0;
            state.rampProgress = 0;
            return;
        }
        if (state.phrasesScored < WARMUP_PHRASES) return;

        var curPct = Math.round(hw.getMastery() * 100);
        if (state.lastObservedMasteryPct != null && curPct !== state.lastObservedMasteryPct) {
            // A panel's slider was moved by a person; retain the established
            // global manual-override behavior rather than fighting that input.
            state.rampDirection = null;
            state.rampProgress = 0;
            state.downStreak = 0;
            settings.autoAdjust = false;
            lsSetDebounced('autoAdjust', false);
            syncControlsUI();
            return;
        }
        var th = thresholds();
        var direction = state.emaHitRate >= th.up ? 'up' : state.emaHitRate <= th.down ? 'down' : null;
        state.downStreak = direction === 'down' && settings.dropResistance ? state.downStreak + 1 : 0;
        if (!direction || (direction === 'down' && settings.dropResistance && state.downStreak < DOWN_CONFIRM_PHRASES)) {
            state.rampDirection = null;
            state.rampProgress = 0;
            return;
        }
        state.lastObservedMasteryPct = curPct;
        if (state.rampDirection !== direction) {
            state.rampDirection = direction;
            state.rampProgress = 0;
        }
        var step = rampStep(th, state.rampProgress);
        var next = Math.max(settings.minMastery, Math.min(settings.maxMastery,
            direction === 'up' ? curPct + step : curPct - step));
        if (next !== curPct && typeof hw.setMastery === 'function') {
            hw.setMastery(next / 100);
            state.lastObservedMasteryPct = next;
            state.rampProgress = (state.rampProgress + 1) % RAMP_PHRASES;
        }
    }

    function tickOneSplitHighway(hw, state) {
        if (!hw || typeof hw.hasPhraseData !== 'function' || !hw.hasPhraseData()) return;
        var provider = typeof hw.getNoteStateProvider === 'function' ? hw.getNoteStateProvider() : null;
        if (!provider) return;
        var phrases = hw.getPhrases();
        if (!phrases || !phrases.length) return;
        var t = hw.getTime();
        if (t < state.lastScoredT - 0.05) { state.noteCursor = 0; state.chordCursor = 0; }
        state.lastScoredT = t;
        var idx = state.curPhraseIdx;
        if (idx < 0 || t < phrases[idx].start_time || t >= phrases[idx].end_time)
            idx = phrases.findIndex(function (p) { return t >= p.start_time && t < p.end_time; });
        if (idx !== state.curPhraseIdx) {
            if (state.curPhraseIdx >= 0 && state.phraseTotal > 0)
                commitSplitPhraseResult(state, hw, state.phraseHits / state.phraseTotal);
            state.curPhraseIdx = idx; state.phraseHits = 0; state.phraseTotal = 0; state.judgedKeys = new Set();
        }
        if (idx < 0) return;
        var phrase = phrases[idx], cutoff = t - 0.6, windowStart = Math.max(phrase.start_time, t - 2);
        function score(items, cursorKey, notesOf) {
            var cursor = state[cursorKey];
            while (cursor < items.length && items[cursor].t < windowStart) cursor++;
            // Only discard entries that have fallen out of the lookback
            // window. Items still in range may be pending ('active'/null) and
            // need another chance to resolve on a later frame.
            state[cursorKey] = cursor;
            for (var scan = cursor; scan < items.length; scan++) {
                var item = items[scan];
                if (item.t > cutoff || item.t >= phrase.end_time) break;
                var notes = notesOf(item);
                for (var ni = 0; ni < notes.length; ni++) {
                    var note = notes[ni], key = judgmentKey(item.t, note.s, note.f);
                    if (state.judgedKeys.has(key)) continue;
                    var result = provider(note, item.t), name = typeof result === 'string' ? result : result && result.state;
                    if (name === 'hit' || name === 'miss') {
                        state.judgedKeys.add(key); state.phraseTotal++;
                        if (name === 'hit') state.phraseHits++;
                    }
                }
            }
        }
        score(typeof hw.getFilteredNotes === 'function' ? hw.getFilteredNotes() : [], 'noteCursor', function (n) { return [n]; });
        score(typeof hw.getFilteredChords === 'function' ? hw.getFilteredChords() : [], 'chordCursor', function (c) { return c.notes || []; });
    }

    function tickSplitScoring() {
        _splitScoreStates.forEach(function (state, hw) { tickOneSplitHighway(hw, state); });
    }

    // ---- Glass-filling HUD (overlay contract: own canvas, own rAF) ----
    var _hudCanvas = null;
    var _hudRafHandle = null;
    var _playerEl = null;   // cached — re-resolved only if disconnected, never per-frame-queried
    var GLASS_W = 26, GLASS_GAP = 8, GLASS_MAX_H = 44, GLASS_MIN_H = 16, LOOKAHEAD = 5;

    function getPlayerEl() {
        if (!_playerEl || !_playerEl.isConnected) _playerEl = document.getElementById('player');
        return _playerEl;
    }

    function ensureHudCanvas() {
        if (_hudCanvas && _hudCanvas.isConnected) return _hudCanvas;
        var player = getPlayerEl();
        if (!player) return null;
        _hudCanvas = document.createElement('canvas');
        _hudCanvas.id = 'dynamic-difficulty-hud';
        _hudCanvas.style.cssText =
            'position:absolute;top:8px;left:50%;transform:translateX(-50%);' +
            'pointer-events:none;z-index:15;';
        player.appendChild(_hudCanvas);
        return _hudCanvas;
    }

    function isPlayerActive() {
        var player = getPlayerEl();
        return !!(player && player.classList.contains('active'));
    }

    function drawHud() {
        if (!isPlayerActive()) {
            if (_hudCanvas) _hudCanvas.style.display = 'none';
            _hudRafHandle = null;
            return;
        }
        _hudRafHandle = requestAnimationFrame(drawHud);

        // Section Map renders the same difficulty glasses in its section bar.
        // Its idempotency marker is a stable capability signal, so this check
        // does not query or mutate DOM on the animation path.
        var sectionMapOwnsGlasses = !!window.__slopsmithSectionMapHooksInstalled;
        var ss = window.feedBackSplitscreen || window.slopsmithSplitscreen;
        if (!settings.showGlasses || sectionMapOwnsGlasses || (ss && typeof ss.isActive === 'function' && ss.isActive())) {
            if (_hudCanvas) _hudCanvas.style.display = 'none';
            return;
        }
        var hw = window.highway;
        if (!hw || typeof hw.hasPhraseData !== 'function' || !hw.hasPhraseData()) {
            if (_hudCanvas) _hudCanvas.style.display = 'none';
            return;
        }
        var canvas = ensureHudCanvas();
        if (!canvas) return;

        var phrases = hw.getPhrases();
        if (!phrases || phrases.length === 0) { canvas.style.display = 'none'; return; }
        canvas.style.display = '';

        var t = hw.getTime();
        var curIdx = phrases.findIndex(function (p) { return t >= p.start_time && t < p.end_time; });
        if (curIdx < 0) curIdx = 0;

        var start = Math.max(0, curIdx - 1);
        var list = phrases.slice(start, start + LOOKAHEAD);
        var maxDiff = 1;
        for (var i = 0; i < phrases.length; i++) maxDiff = Math.max(maxDiff, phrases[i].max_difficulty);
        var mastery = typeof hw.getMastery === 'function' ? hw.getMastery() : 0;

        var w = Math.max(1, list.length * (GLASS_W + GLASS_GAP) - GLASS_GAP);
        var h = GLASS_MAX_H + 12;
        var dpr = window.devicePixelRatio || 1;
        var wantW = Math.round(w * dpr), wantH = Math.round(h * dpr);
        if (canvas.width !== wantW || canvas.height !== wantH) {
            canvas.width = wantW;
            canvas.height = wantH;
            canvas.style.width = w + 'px';
            canvas.style.height = h + 'px';
        }
        var ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);

        list.forEach(function (p, i2) {
            var sizeFrac = Math.max(0.3, p.max_difficulty / maxDiff);
            var glassH = GLASS_MIN_H + (GLASS_MAX_H - GLASS_MIN_H) * sizeFrac;
            var idxLevel = p.max_difficulty > 0
                ? Math.min(p.max_difficulty, Math.floor(mastery * (p.max_difficulty + 1)))
                : 0;
            var fillFrac = p.max_difficulty > 0 ? (idxLevel / p.max_difficulty) : 1;
            var x = i2 * (GLASS_W + GLASS_GAP);
            var y = h - glassH - 4;
            var isCurrent = (start + i2) === curIdx;

            ctx.lineWidth = isCurrent ? 2 : 1;
            ctx.strokeStyle = isCurrent ? '#e8c040' : 'rgba(200,200,200,0.5)';
            ctx.beginPath();
            if (typeof ctx.roundRect === 'function') ctx.roundRect(x, y, GLASS_W, glassH, 4);
            else ctx.rect(x, y, GLASS_W, glassH);
            ctx.stroke();

            var fillH = Math.max(0, glassH * fillFrac - 1);
            if (fillH > 0) {
                ctx.fillStyle = fillFrac > 0.8 ? 'rgba(224,80,80,0.55)'
                    : fillFrac > 0.4 ? 'rgba(232,192,64,0.55)'
                        : 'rgba(64,128,224,0.55)';
                ctx.fillRect(x + 1, y + glassH - fillH, GLASS_W - 2, fillH);
            }
        });
    }

    // ---- Generate-difficulties CTA (calls routes.py's /generate) ----
    var _generateBtn = null;
    var _generating = false;   // double-submit guard — Slopsmith's editor plugin
                                // shipped without one on its Build button and a
                                // stray second click raced two concurrent jobs
    var _generateLabelTimer = null;

    function currentTargetStatus() {
        var hw = window.highway;
        if (!hw || typeof hw.getSongInfo !== 'function') {
            return { ok: false, reason: 'unavailable' };
        }
        // highway.getSongInfo() is chart metadata only.  In particular, its
        // song_info payload has no filename; the host publishes that separately
        // as feedBack.currentSong.filename.  Requiring si.filename here made
        // every real host song look unloaded, despite the player being active.
        var si = hw.getSongInfo() || {};
        var currentSong = (window.feedBack && window.feedBack.currentSong) || {};
        var filename = currentSong.filename || si.filename;
        if (!filename) return { ok: false, reason: 'unloaded' };
        // Highway's snake_case index describes the currently streamed
        // arrangement.  currentSong uses camelCase and is the fallback for
        // hosts that expose only the plugin-context object.
        var arrangementIndex = si.arrangement_index;
        if (arrangementIndex == null) arrangementIndex = currentSong.arrangementIndex;
        if (arrangementIndex == null) arrangementIndex = 0;
        // Defensive clamp at point of use (settings.generateLevels came from
        // localStorage and could be stale/out-of-range) — same convention as
        // thresholds()/emaAlpha() clamping settings.sensitivity/reactionSpeed
        // rather than trusting the stored value blindly. Parse once and default
        // only on NaN — `|| 4` would also catch a legitimately parsed 0.
        var parsedLevels = parseInt(settings.generateLevels, 10);
        var levels = Math.max(2, Math.min(8, isNaN(parsedLevels) ? 4 : parsedLevels));
        return {
            ok: true,
            target: {
                filename: filename,
                arrangement_index: arrangementIndex,
                levels: levels,
            },
        };
    }

    function currentTarget() {
        var status = currentTargetStatus();
        return status.ok ? status.target : null;
    }

    function updateGenerateButtonVisibility() {
        if (!_generateBtn) return;
        var hw = window.highway;
        var hasData = !!(hw && typeof hw.hasPhraseData === 'function' && hw.hasPhraseData());
        _generateBtn.style.display = hasData ? 'none' : '';
    }

    function _resetGenerateBtnLabel() {
        if (_generateBtn) _generateBtn.textContent = '⚙️ Generate Difficulties';
    }

    function _clearGenerateLabelTimer() {
        if (_generateLabelTimer) clearTimeout(_generateLabelTimer);
        _generateLabelTimer = null;
    }

    // Single choke point for every "⚙️ Generate Difficulties" label change —
    // all five states the button can show (idle, unavailable/no-song,
    // generating, failed, skipped) go through this, so _generateLabelTimer
    // only ever has one owner. `resetDelay` omitted/null means the label
    // sticks (no scheduled revert); `resetFn` defaults to restoring the
    // idle "⚙️ Generate Difficulties" text.
    function setGenerateLabel(text, resetDelay, resetFn) {
        if (!_generateBtn) return;
        _generateBtn.textContent = text;
        _clearGenerateLabelTimer();
        if (resetDelay != null) {
            _generateLabelTimer = setTimeout(function () {
                _generateLabelTimer = null;
                (resetFn || _resetGenerateBtnLabel)();
            }, resetDelay);
        }
    }

    async function onGenerateClick() {
        if (_generating) return; // guard: one in-flight generate at a time
        var status = currentTargetStatus();
        if (!status.ok) {
            var unavailable = status.reason === 'unavailable';
            console.warn(
                unavailable
                    ? '[difficulty_ladder] generate click ignored: highway.getSongInfo() is unavailable'
                    : '[difficulty_ladder] generate click ignored: no song loaded yet (highway.getSongInfo() returned nothing)'
            );
            setGenerateLabel(unavailable ? 'Player unavailable' : 'No song loaded', 2000);
            return;
        }
        var target = status.target;
        _generating = true;
        _generateBtn.disabled = true;
        setGenerateLabel('Generating…', null);
        try {
            var resp = await fetch('/api/plugins/' + PLUGIN_ID + '/generate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(target),
            });
            var data = null;
            try { data = await resp.json(); } catch (_) { /* noop */ }
            if (!resp.ok || !data || data.error) {
                console.warn('[difficulty_ladder] generate failed:', (data && data.error) || resp.status);
                setGenerateLabel('Generate failed', 2500);
                return;
            }
            // /generate processes the full song.  A pack can mix guitar,
            // bass and keys arrangements; routes.py classifies each one and
            // intentionally skips drums.  Do not treat a partial skip as a
            // failure when other arrangements were generated successfully.
            if (data.generated === 0) {
                setGenerateLabel(
                    data.failed ? 'Generate failed' : 'Difficulties already exist',
                    2500, updateGenerateButtonVisibility
                );
                return;
            }
            setGenerateLabel(
                data.generated === 1 ? 'Generated 1 arrangement' : 'Generated ' + data.generated + ' arrangements',
                2500, updateGenerateButtonVisibility
            );
            // Reload the current song so the highway WS re-streams the new
            // phrase data (it was written server-side after this song's
            // websocket already sent its snapshot).
            var hw = window.highway;
            if (hw && typeof hw.reconnect === 'function') {
                hw.reconnect(target.filename, target.arrangement_index);
            }
        } catch (e) {
            console.warn('[difficulty_ladder] generate request failed:', e);
            setGenerateLabel('Generate failed', 2500);
        } finally {
            _generating = false;
            _generateBtn.disabled = false;
        }
    }

    // ---- Player-controls toggle (v3 chrome contract) ----
    var _controlsBtn = null;
    function syncControlsUI() {
        if (!_controlsBtn) return;
        _controlsBtn.classList.toggle('fb-primary', settings.autoAdjust);
        _controlsBtn.style.opacity = settings.autoAdjust ? '1' : '0.6';
        _controlsBtn.title = settings.autoAdjust
            ? 'Difficulty Ladder: auto-adjusting from your accuracy (click to pause)'
            : 'Difficulty Ladder: paused (click to resume auto-adjust)';
    }

    function mountControls() {
        if (!window.feedBack || window.feedBack.uiVersion !== 'v3') return;
        if (!window.feedBack.ui || typeof window.feedBack.ui.playerControlSlot !== 'function') return;
        var slot = window.feedBack.ui.playerControlSlot();
        if (!slot) return;
        if (_controlsBtn && slot.contains(_controlsBtn)) {
            syncControlsUI();
            updateGenerateButtonVisibility();
            return;
        }

        _controlsBtn = document.createElement('button');
        _controlsBtn.id = 'dynamic-difficulty-toggle';
        _controlsBtn.className = 'fb-text text-xs px-2 py-1 rounded hover:bg-white/10 flex items-center gap-1';
        _controlsBtn.textContent = '🥃 Auto-Difficulty';
        _controlsBtn.onclick = function () {
            settings.autoAdjust = !settings.autoAdjust;
            lsSet('autoAdjust', settings.autoAdjust);
            _lastObservedMasteryPct = null;
            syncControlsUI();
        };
        slot.appendChild(_controlsBtn);
        syncControlsUI();

        _generateBtn = document.createElement('button');
        _generateBtn.id = 'dynamic-difficulty-generate';
        _generateBtn.className = 'fb-text text-xs px-2 py-1 rounded hover:bg-white/10 flex items-center gap-1';
        _generateBtn.textContent = '⚙️ Generate Difficulties';
        _generateBtn.title = 'Generate difficulty ladders for every non-drum arrangement in this song (sloppak songs only)';
        _generateBtn.onclick = onGenerateClick;
        slot.appendChild(_generateBtn);
        updateGenerateButtonVisibility();
    }

    // ---- Lifecycle ----
    function startRafLoops() {
        if (!_scoreRafHandle) tickScoring();
        if (!_hudRafHandle) drawHud();
    }

    function onSongEvent() {
        ensureMasterySaveHook();
        var hw = window.highway;
        var si = (hw && typeof hw.getSongInfo === 'function') ? hw.getSongInfo() : null;
        var key = songKeyOf(si);
        if (key !== _songKey) {
            _songKey = key;
            _songInstrument = null;
            resetPerSongState();
            _maybeRestoreSongMastery(key);
        }
        mountControls();
        updateGenerateButtonVisibility();
        startRafLoops();
        contributeDiagnostics();
        // Emit section difficulty data for sectionmap plugin
        calculateAndEmitSectionDifficulties();
    }

    // Bind late (rule 21's "register into the host" pattern, applied here):
    // plugins load alphabetically and window.feedBack.libraryCardActions may
    // not exist the instant this script runs, so try now and again on the
    // next couple of lifecycle events that fire regardless of whether the
    // user ever opens the player — registerLibraryCardBadge() is idempotent,
    // so extra calls after the first success are free no-ops.
    registerLibraryCardBadge();

    if (window.feedBack && typeof window.feedBack.on === 'function') {
        window.feedBack.on('song:ready', onSongEvent);
        // Split Screen emits this after its panel highways are created and
        // again after a canvas/highway replacement.  By then Note Detect is
        // normally loaded; wrapping its public factory lets Ladder observe
        // future per-panel Detect clicks without depending on Split Screen
        // internals.
        startSplitScreenHookSubscription();
        window.feedBack.on('library:changed', registerLibraryCardBadge);
        window.feedBack.on('highway:created', mountControls);
        window.feedBack.on('highway:visibility', function (ev) {
            var detail = ev && ev.detail;
            if (detail && detail.visible) {
                startSplitScreenHookSubscription();
                installSplitScreenDetectorHook();
                startRafLoops();
            } else {
                stopSplitScreenHookSubscription();
                if (_scoreRafHandle) { cancelAnimationFrame(_scoreRafHandle); _scoreRafHandle = null; }
                if (_hudRafHandle) { cancelAnimationFrame(_hudRafHandle); _hudRafHandle = null; }
                _clearGenerateLabelTimer();
                if (_hudCanvas) _hudCanvas.style.display = 'none';
                if (_generateLabelTimer) { clearTimeout(_generateLabelTimer); _generateLabelTimer = null; }
            }
        });
    }

    // Safety net: if highway:visibility is not fired for every player-active
    // transition (e.g. pause/resume without a song change), the document
    // visibilitychange event ensures we restart both loops whenever the tab
    // returns to the foreground while the player is active.
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') startRafLoops();
    });

    // Settings panel writes localStorage directly (see settings.html) and
    // notifies us to re-read rather than us polling localStorage per frame.
    window.addEventListener('storage', function (e) {
        if (!e.key || e.key.indexOf(LS_PREFIX) !== 0) return;
        var short = e.key.slice(LS_PREFIX.length);
        if (Object.prototype.hasOwnProperty.call(settings, short)) {
            try { settings[short] = JSON.parse(e.newValue); } catch (_) {
                if (short === 'dropResistance') settings[short] = false;
            }
            if (short === 'dropResistance') {
                settings[short] = settings[short] === true;
                _downStreak = 0;
            }
            syncControlsUI();
            contributeDiagnostics();
        }
    });
    window.addEventListener(PLUGIN_ID + ':settings-changed', function (ev) {
        var patch = ev && ev.detail;
        if (!patch) return;
        Object.assign(settings, patch);
        if (Object.prototype.hasOwnProperty.call(patch, 'dropResistance')) {
            settings.dropResistance = patch.dropResistance === true;
            _downStreak = 0;
        }
        syncControlsUI();
        contributeDiagnostics();
    });

    // Node-only export hook for tests (mirrors the convention used by
    // feedBack-plugin-sectionmap's screen.js): expose the pure/DOM-light
    // helpers so they're unit-testable without a browser, and skip the
    // side-effect wiring below (event-bus subscriptions, rAF loops) since
    // there's no real window.feedBack/highway to wire up against in Node.
    // Browsers never hit this branch (`module` is undefined), so runtime
    // behavior is unchanged.
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            thresholds: thresholds, emaAlpha: emaAlpha, songKeyOf: songKeyOf,
            judgmentKey: judgmentKey, settings: settings,
            _dominantSongMastery: _dominantSongMastery,
            _masteryPct: _masteryPct, _rememberSongInstrument: _rememberSongInstrument,
            loadSongMasteryMap: loadSongMasteryMap, saveSongMasteryMap: saveSongMasteryMap,
            calculateAndEmitSectionDifficulties: calculateAndEmitSectionDifficulties,
            commitPhraseResult: commitPhraseResult, resetPerSongState: resetPerSongState,
            newSplitScoreState: newSplitScoreState, commitSplitPhraseResult: commitSplitPhraseResult,
            tickOneSplitHighway: tickOneSplitHighway,
            rampStep: rampStep, WARMUP_PHRASES: WARMUP_PHRASES, RAMP_PHRASES: RAMP_PHRASES,
            currentTarget: currentTarget, currentTargetStatus: currentTargetStatus,
            mountControls: mountControls, onGenerateClick: onGenerateClick,
        };
        return;
    }

    ensureMasterySaveHook();
    installSplitScreenDetectorHook();
    startRafLoops();
})();
