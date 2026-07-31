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

    function lsGet(key, def) {
        try {
            var v = localStorage.getItem(LS_PREFIX + key);
            return v === null ? def : JSON.parse(v);
        } catch (_) { return def; }
    }
    function lsSet(key, val) {
        try { localStorage.setItem(LS_PREFIX + key, JSON.stringify(val)); } catch (_) { /* noop */ }
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
            var v = map[k];
            if (typeof v !== 'number' || !isFinite(v)) continue;
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
        if (map[_songKey] === pct) return;
        map[_songKey] = pct;
        saveSongMasteryMap(map);
    }

    // Called once per song change (see onSongEvent). Applies this song's own
    // remembered difficulty, if any, over whatever global value core just
    // carried over from the previous song.
    function _maybeRestoreSongMastery(key) {
        if (!key) return;
        var hw = window.highway;
        if (!hw || typeof hw.hasPhraseData !== 'function' || !hw.hasPhraseData()) return;
        var saved = loadSongMasteryMap()[key];
        if (typeof saved !== 'number' || !isFinite(saved)) return;
        if (typeof window.setMastery === 'function') window.setMastery(saved);
    }

    // ---- Settings (localStorage-backed; see settings.html for the panel) ----
    var settings = {
        autoAdjust: lsGet('autoAdjust', false),
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

    // ---- Per-song scoring state ----
    var _songKey = null;
    var _emaHitRate = null;        // null = no phrase scored yet this song
    // EMA weight is now the reactionSpeed setting (emaAlpha(), above) rather
    // than a hardcoded constant — see issue #5. Read live (not cached) since
    // the settings-changed listener below can update settings.reactionSpeed
    // mid-song.
    var _judgedKeys = null;        // Set, reset every phrase to bound memory
    var _phraseHits = 0;
    var _phraseTotal = 0;
    var _curPhraseIdx = -1;
    var _lastAutoAppliedPct = null; // used to detect a manual slider override
    var _lastAutoAction = null;     // { direction, pct, reason } — for diagnostics
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
        _curPhraseIdx = -1;
        _lastAutoAppliedPct = null;
        _lastAutoAction = null;
        _noteCursor = 0;
        _chordCursor = 0;
        _lastScoredT = -1;
    }
    resetPerSongState();

    function judgmentKey(time, s, f) { return time + '_' + s + '_' + f; }

    function commitPhraseResult(ratio) {
        var alpha = emaAlpha();
        _emaHitRate = (_emaHitRate == null) ? ratio : (alpha * ratio + (1 - alpha) * _emaHitRate);
        if (!settings.autoAdjust) {
            contributeDiagnostics();
            return;
        }
        var hw = window.highway;
        if (!hw || typeof hw.getMastery !== 'function') {
            contributeDiagnostics();
            return;
        }

        var curPct = Math.round(hw.getMastery() * 100);
        // Manual-override doctrine: a human action always wins over automation.
        // If the live value has drifted from what we last applied, someone
        // moved the slider themselves — stand down instead of fighting them.
        if (_lastAutoAppliedPct != null && curPct !== _lastAutoAppliedPct) {
            settings.autoAdjust = false;
            lsSet('autoAdjust', false);
            syncControlsUI();
            contributeDiagnostics();
            return;
        }

        var th = thresholds();
        var next = curPct;
        if (_emaHitRate >= th.up) next = curPct + th.step;
        else if (_emaHitRate <= th.down) next = curPct - th.step;
        next = Math.max(settings.minMastery, Math.min(settings.maxMastery, next));

        if (next !== curPct && typeof window.setMastery === 'function') {
            window.setMastery(next);
            _lastAutoAppliedPct = next;
            var dir = next > curPct ? 'up' : 'down';
            _lastAutoAction = {
                direction: dir,
                pct: next,
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
                if (cname === 'hit' || cname === 'miss') {
                    _judgedKeys.add(ck);
                    _phraseTotal++;
                    if (cname === 'hit') _phraseHits++;
                }
            }
        }
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

        if (!settings.showGlasses) {
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

    function currentTarget() {
        var hw = window.highway;
        var si = hw && typeof hw.getSongInfo === 'function' ? hw.getSongInfo() : null;
        if (!si || !si.filename) return null;
        // Defensive clamp at point of use (settings.generateLevels came from
        // localStorage and could be stale/out-of-range) — same convention as
        // thresholds()/emaAlpha() clamping settings.sensitivity/reactionSpeed
        // rather than trusting the stored value blindly. Preserves the
        // existing target shape/behavior; only adds this one field.
        var levels = Math.max(2, Math.min(8, parseInt(settings.generateLevels, 10) || 4));
        return { filename: si.filename, arrangement_index: si.arrangement_index || 0, levels: levels };
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

    async function onGenerateClick() {
        if (_generating) return; // guard: one in-flight generate at a time
        var target = currentTarget();
        if (!target) return;
        _generating = true;
        _generateBtn.disabled = true;
        _generateBtn.textContent = 'Generating…';
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
                _generateBtn.textContent = 'Generate failed';
                setTimeout(_resetGenerateBtnLabel, 2500);
                return;
            }
            if (data.skipped) {
                _generateBtn.textContent = data.skipped === 'already-has-phrases'
                    ? 'Already has difficulties' : 'Not enough content';
                setTimeout(updateGenerateButtonVisibility, 2500);
                return;
            }
            // Reload the current song so the highway WS re-streams the new
            // phrase data (it was written server-side after this song's
            // websocket already sent its snapshot).
            var hw = window.highway;
            if (hw && typeof hw.reconnect === 'function') {
                hw.reconnect(target.filename, target.arrangement_index);
            }
        } catch (e) {
            console.warn('[difficulty_ladder] generate request failed:', e);
            _generateBtn.textContent = 'Generate failed';
            setTimeout(_resetGenerateBtnLabel, 2500);
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
            _lastAutoAppliedPct = null;
            syncControlsUI();
        };
        slot.appendChild(_controlsBtn);
        syncControlsUI();

        _generateBtn = document.createElement('button');
        _generateBtn.id = 'dynamic-difficulty-generate';
        _generateBtn.className = 'fb-text text-xs px-2 py-1 rounded hover:bg-white/10 flex items-center gap-1';
        _generateBtn.textContent = '⚙️ Generate Difficulties';
        _generateBtn.title = 'Generate an Easy/Medium/Hard difficulty ladder for this arrangement (sloppak songs only)';
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
        window.feedBack.on('library:changed', registerLibraryCardBadge);
        window.feedBack.on('highway:created', mountControls);
        window.feedBack.on('highway:visibility', function (ev) {
            var detail = ev && ev.detail;
            if (detail && detail.visible) {
                startRafLoops();
            } else {
                if (_scoreRafHandle) { cancelAnimationFrame(_scoreRafHandle); _scoreRafHandle = null; }
                if (_hudRafHandle) { cancelAnimationFrame(_hudRafHandle); _hudRafHandle = null; }
                if (_hudCanvas) _hudCanvas.style.display = 'none';
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
        if (short in settings) {
            try { settings[short] = JSON.parse(e.newValue); } catch (_) { /* noop */ }
            syncControlsUI();
            contributeDiagnostics();
        }
    });
    window.addEventListener(PLUGIN_ID + ':settings-changed', function (ev) {
        var patch = ev && ev.detail;
        if (!patch) return;
        Object.assign(settings, patch);
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
            loadSongMasteryMap: loadSongMasteryMap, saveSongMasteryMap: saveSongMasteryMap,
            calculateAndEmitSectionDifficulties: calculateAndEmitSectionDifficulties,
            currentTarget: currentTarget,
        };
        return;
    }

    ensureMasterySaveHook();
    startRafLoops();
})();
