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

    var PLUGIN_ID = 'dynamic_difficulty';
    var LS_PREFIX = 'dynamic_difficulty.';

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

    function loadSongMasteryMap() {
        try {
            var parsed = JSON.parse(localStorage.getItem(SONG_MASTERY_LS_KEY) || '{}');
            return (parsed && typeof parsed === 'object') ? parsed : {};
        } catch (_) { return {}; }
    }
    function saveSongMasteryMap(map) {
        try { localStorage.setItem(SONG_MASTERY_LS_KEY, JSON.stringify(map)); } catch (_) { /* noop */ }
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
        sensitivity: lsGet('sensitivity', 2),   // 1 (cautious) .. 3 (aggressive)
        minMastery: lsGet('minMastery', 0),     // percent
        maxMastery: lsGet('maxMastery', 100),   // percent
    };

    function thresholds() {
        var s = Math.max(1, Math.min(3, settings.sensitivity));
        return {
            up: 0.93 - (s - 1) * 0.05,     // 1:0.93  2:0.88  3:0.83
            down: 0.65 + (s - 1) * 0.03,   // 1:0.65  2:0.68  3:0.71
            step: 10 + (s - 1) * 5,        // percent step: 10 / 15 / 20
        };
    }

    // ---- Per-song scoring state ----
    var _songKey = null;
    var _emaHitRate = null;        // null = no phrase scored yet this song
    var EMA_ALPHA = 0.35;
    var _judgedKeys = null;        // Set, reset every phrase to bound memory
    var _phraseHits = 0;
    var _phraseTotal = 0;
    var _curPhraseIdx = -1;
    var _lastAutoAppliedPct = null; // used to detect a manual slider override
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
        _noteCursor = 0;
        _chordCursor = 0;
        _lastScoredT = -1;
    }
    resetPerSongState();

    function judgmentKey(time, s, f) { return time + '_' + s + '_' + f; }

    function commitPhraseResult(ratio) {
        _emaHitRate = (_emaHitRate == null) ? ratio : (EMA_ALPHA * ratio + (1 - EMA_ALPHA) * _emaHitRate);
        if (!settings.autoAdjust) return;
        var hw = window.highway;
        if (!hw || typeof hw.getMastery !== 'function') return;

        var curPct = Math.round(hw.getMastery() * 100);
        // Manual-override doctrine: a human action always wins over automation.
        // If the live value has drifted from what we last applied, someone
        // moved the slider themselves — stand down instead of fighting them.
        if (_lastAutoAppliedPct != null && curPct !== _lastAutoAppliedPct) {
            settings.autoAdjust = false;
            lsSet('autoAdjust', false);
            syncControlsUI();
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
        }
    }

    // Reads live per-note judgments through the note-state provider slot
    // (owned by whichever scorer plugin, e.g. note_detect, is active). This
    // is a read, not a takeover — highway.getNoteStateProvider() is a public
    // getter documented for exactly this kind of consumption.
    var _scoreRafHandle = null;
    function tickScoring() {
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
        _hudRafHandle = requestAnimationFrame(drawHud);

        if (!settings.showGlasses || !isPlayerActive()) {
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
        return { filename: si.filename, arrangement_index: si.arrangement_index || 0 };
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
                console.warn('[dynamic_difficulty] generate failed:', (data && data.error) || resp.status);
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
            console.warn('[dynamic_difficulty] generate request failed:', e);
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
            ? 'Dynamic Difficulty: auto-adjusting from your accuracy (click to pause)'
            : 'Dynamic Difficulty: paused (click to resume auto-adjust)';
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
    }

    if (window.feedBack && typeof window.feedBack.on === 'function') {
        window.feedBack.on('song:ready', onSongEvent);
        window.feedBack.on('highway:created', mountControls);
    }

    // Settings panel writes localStorage directly (see settings.html) and
    // notifies us to re-read rather than us polling localStorage per frame.
    window.addEventListener('storage', function (e) {
        if (!e.key || e.key.indexOf(LS_PREFIX) !== 0) return;
        var short = e.key.slice(LS_PREFIX.length);
        if (short in settings) {
            try { settings[short] = JSON.parse(e.newValue); } catch (_) { /* noop */ }
            syncControlsUI();
        }
    });
    window.addEventListener(PLUGIN_ID + ':settings-changed', function (ev) {
        var patch = ev && ev.detail;
        if (!patch) return;
        Object.assign(settings, patch);
        syncControlsUI();
    });

    ensureMasterySaveHook();
    if (!_scoreRafHandle) tickScoring();
    if (!_hudRafHandle) drawHud();
})();
