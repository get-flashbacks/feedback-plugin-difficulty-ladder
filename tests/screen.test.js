'use strict';
// Coverage for pure/DOM-light helpers in screen.js: sensitivity thresholds,
// per-song key derivation, judgment-dedup key shape.
// Runs under the org reusable CI as `node tests/screen.test.js` (mirrors the
// convention in feedBack-plugin-sectionmap's tests/screen.test.js).
//
// This file also documents (issue #8) the phrase-data contract this plugin
// shares with feedBack-plugin-sectionmap: both plugins independently read
// window.highway.getPhrases() / hasPhraseData() / getMastery() — see
// COMPLIANCE.md. The shape-parity test below guards that contract from
// silently drifting on either side.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

function freshPlugin({ stored = {} } = {}) {
    const listeners = new Map();
    const store = new Map(Object.entries(stored));
    global.window = {
        addEventListener(type, listener) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(listener);
        },
        dispatchEvent(event) {
            for (const listener of listeners.get(event.type) || []) listener(event);
        },
    };
    global.document = {
        addEventListener: () => {},
        getElementById: () => null,
    };
    global.localStorage = {
        getItem: (key) => store.has(key) ? store.get(key) : null,
        setItem: (key, value) => store.set(key, String(value)),
    };
    const file = path.join(__dirname, '..', 'screen.js');
    delete require.cache[require.resolve(file)];
    return require(file);
}

test('thresholds() at sensitivity 2 (default) matches the documented up/down/step values', () => {
    const mod = freshPlugin();
    mod.settings.sensitivity = 2;
    assert.deepEqual(mod.thresholds(), { up: 0.88, down: 0.68, step: 15 });
});

function assertThresholdsClose(actual, expected) {
    assert.ok(Math.abs(actual.up - expected.up) < 1e-9, `up: ${actual.up} ~= ${expected.up}`);
    assert.ok(Math.abs(actual.down - expected.down) < 1e-9, `down: ${actual.down} ~= ${expected.down}`);
    assert.equal(actual.step, expected.step);
}

test('thresholds() clamps sensitivity to [1,3]', () => {
    const mod = freshPlugin();
    mod.settings.sensitivity = 1;
    assertThresholdsClose(mod.thresholds(), { up: 0.93, down: 0.65, step: 10 });
    mod.settings.sensitivity = 3;
    assertThresholdsClose(mod.thresholds(), { up: 0.83, down: 0.71, step: 20 });
    mod.settings.sensitivity = 99; // out of range -> clamps to 3's values
    assertThresholdsClose(mod.thresholds(), { up: 0.83, down: 0.71, step: 20 });
});

test('songKeyOf combines filename and arrangement_index so per-song state never leaks across arrangements', () => {
    const mod = freshPlugin();
    assert.equal(mod.songKeyOf({ filename: 'song.feedpak', arrangement_index: 0 }), 'song.feedpak::0');
    assert.equal(mod.songKeyOf({ filename: 'song.feedpak', arrangement_index: 1 }), 'song.feedpak::1');
});

test('songKeyOf falls back to the arrangement name when arrangement_index is absent', () => {
    const mod = freshPlugin();
    assert.equal(mod.songKeyOf({ filename: 'song.feedpak', arrangement: 'Bass' }), 'song.feedpak::Bass');
});

test('songKeyOf returns null for no song info (no song loaded yet)', () => {
    const mod = freshPlugin();
    assert.equal(mod.songKeyOf(null), null);
    assert.equal(mod.songKeyOf(undefined), null);
});

test('currentTargetStatus distinguishes missing highway API from no loaded song', () => {
    const mod = freshPlugin();
    assert.deepEqual(mod.currentTargetStatus(), { ok: false, reason: 'unavailable' });

    global.window.highway = { getSongInfo: () => null };
    assert.deepEqual(mod.currentTargetStatus(), { ok: false, reason: 'unloaded' });
});

test('currentTargetStatus returns the generate target when a song is loaded', () => {
    const mod = freshPlugin();
    mod.settings.generateLevels = 99;
    global.window.highway = {
        getSongInfo: () => ({ filename: 'song.feedpak', arrangement_index: 2 }),
    };

    assert.deepEqual(mod.currentTargetStatus(), {
        ok: true,
        target: { filename: 'song.feedpak', arrangement_index: 2, levels: 8 },
    });
});

test('publishes the Section Map compatibility capability after the plugin rename', () => {
    freshPlugin();
    assert.equal(global.window._ddCapabilities.sectionDifficulty, true);
});

test('currentTargetStatus uses feedBack.currentSong filename with the real highway song-info shape', () => {
    const mod = freshPlugin();
    global.window.feedBack = {
        currentSong: { filename: 'library/song.feedpak', arrangementIndex: 3 },
    };
    global.window.highway = {
        // Core's song_info deliberately has no filename.
        getSongInfo: () => ({ title: 'Song', arrangement_index: 3 }),
    };

    assert.deepEqual(mod.currentTargetStatus(), {
        ok: true,
        target: { filename: 'library/song.feedpak', arrangement_index: 3, levels: 4 },
    });
});

test('judgmentKey is stable and distinct per (time, string, fret)', () => {
    const mod = freshPlugin();
    assert.equal(mod.judgmentKey(1.5, 2, 3), '1.5_2_3');
    assert.notEqual(mod.judgmentKey(1.5, 2, 3), mod.judgmentKey(1.5, 3, 2));
});

// ── reactionSpeed / EMA_ALPHA (issue #5) ────────────────────────────────────

test('emaAlpha() at the default reactionSpeed (2) matches the plugin\'s original hardcoded EMA_ALPHA (0.35)', () => {
    const mod = freshPlugin();
    mod.settings.reactionSpeed = 2;
    assert.ok(Math.abs(mod.emaAlpha() - 0.35) < 1e-9);
});

test('emaAlpha() clamps reactionSpeed to [1,3] and spans slow(0.20)..fast(0.50)', () => {
    const mod = freshPlugin();
    mod.settings.reactionSpeed = 1;
    assert.ok(Math.abs(mod.emaAlpha() - 0.20) < 1e-9);
    mod.settings.reactionSpeed = 3;
    assert.ok(Math.abs(mod.emaAlpha() - 0.50) < 1e-9);
    mod.settings.reactionSpeed = 0; // out of range -> clamps to 1's value
    assert.ok(Math.abs(mod.emaAlpha() - 0.20) < 1e-9);
});

test('emaAlpha() and thresholds() are independent axes (sensitivity does not move emaAlpha and vice versa)', () => {
    const mod = freshPlugin();
    mod.settings.sensitivity = 3;
    mod.settings.reactionSpeed = 1;
    const th = mod.thresholds();
    assert.equal(th.step, 20); // driven only by sensitivity
    assert.ok(Math.abs(mod.emaAlpha() - 0.20) < 1e-9); // driven only by reactionSpeed
});

// ── Library card badge (issue #4) ───────────────────────────────────────────

test('_dominantSongMastery returns null for a song with no saved mastery', () => {
    const mod = freshPlugin();
    assert.equal(mod._dominantSongMastery({ filename: 'unplayed.feedpak' }), null);
});

test('_dominantSongMastery returns null for a song/undefined with no filename', () => {
    const mod = freshPlugin();
    assert.equal(mod._dominantSongMastery(null), null);
    assert.equal(mod._dominantSongMastery({}), null);
});

test('_dominantSongMastery prefers arrangement 0 when multiple arrangements have saved values', () => {
    const mod = freshPlugin();
    mod.saveSongMasteryMap({ 'song.feedpak::0': 40, 'song.feedpak::1': 90 });
    assert.equal(mod._dominantSongMastery({ filename: 'song.feedpak' }), 40);
});

test('_dominantSongMastery falls back to whichever arrangement has a value when arrangement 0 has none', () => {
    const mod = freshPlugin();
    mod.saveSongMasteryMap({ 'song.feedpak::1': 65 });
    assert.equal(mod._dominantSongMastery({ filename: 'song.feedpak' }), 65);
});

test('_dominantSongMastery does not match a different song sharing a filename prefix', () => {
    const mod = freshPlugin();
    mod.saveSongMasteryMap({ 'song.feedpak::0': 40 });
    assert.equal(mod._dominantSongMastery({ filename: 'song' }), null); // 'song' is not a prefix match of 'song.feedpak::0'
});

test('mastery readers accept instrument-tagged records without breaking legacy numeric records', () => {
    const mod = freshPlugin();
    mod.saveSongMasteryMap({
        'keys.feedpak::0': { mastery: 72, instrument: 'keys' },
        'guitar.feedpak::0': 64,
    });
    assert.equal(mod._dominantSongMastery({ filename: 'keys.feedpak' }), 72);
    assert.equal(mod._dominantSongMastery({ filename: 'guitar.feedpak' }), 64);
});

test('_rememberSongInstrument upgrades an existing mastery record and preserves its identifiers', () => {
    const mod = freshPlugin();
    mod.saveSongMasteryMap({ 'song.feedpak::2': 81 });

    mod._rememberSongInstrument('song.feedpak::2', 'fretted');

    assert.deepEqual(mod.loadSongMasteryMap(), {
        'song.feedpak::2': { mastery: 81, instrument: 'fretted' },
    });
});

test('_rememberSongInstrument persists classification before the first mastery value exists', () => {
    const mod = freshPlugin();

    mod._rememberSongInstrument('new.feedpak::0', 'keys');

    assert.deepEqual(mod.loadSongMasteryMap(), {
        'new.feedpak::0': { mastery: null, instrument: 'keys' },
    });
    assert.equal(mod._dominantSongMastery({ filename: 'new.feedpak' }), null);
});

// ── Auto-adjust warm-up window + ramped stepping ────────────────────────────
// (Rocksmith-comparison audit follow-up: a fresh song no longer acts before
// WARMUP_PHRASES phrases are scored, and a qualifying streak now ramps
// mastery by rampStep() per phrase instead of jumping the full th.step in
// one call — see ROCKSMITH_COMPARISON.md.)

function attachHighwayStub(initialPct) {
    let masteryFrac = initialPct / 100;
    const calls = [];
    global.window.highway = { getMastery: () => masteryFrac };
    global.window.setMastery = (pct) => { calls.push(pct); masteryFrac = pct / 100; };
    return calls;
}

function setDropResistance(value) {
    global.localStorage.setItem('difficulty_ladder.dropResistance', JSON.stringify(value));
    global.window.dispatchEvent({
        type: 'difficulty_ladder:settings-changed',
        detail: { dropResistance: value },
    });
}

test('rampStep() increments total the exact full step at every sensitivity', () => {
    const mod = freshPlugin();
    mod.settings.sensitivity = 1;
    assert.deepEqual([0, 1, 2].map(i => mod.rampStep(mod.thresholds(), i)), [3, 4, 3]);
    mod.settings.sensitivity = 2;
    assert.deepEqual([0, 1, 2].map(i => mod.rampStep(mod.thresholds(), i)), [5, 5, 5]);
    mod.settings.sensitivity = 3;
    assert.deepEqual([0, 1, 2].map(i => mod.rampStep(mod.thresholds(), i)), [7, 6, 7]);
});

test('rampStep() never returns less than 1', () => {
    const mod = freshPlugin();
    assert.equal(mod.rampStep({ step: 1 }), 1);
    assert.equal(mod.rampStep({ step: 0 }), 1);
});

test('commitPhraseResult() does not act before WARMUP_PHRASES phrases have been scored', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 2;
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES - 1; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls.length, 0, 'no auto-adjust call before warm-up is satisfied');
    mod.commitPhraseResult(1.0); // the WARMUP_PHRASES-th qualifying phrase
    assert.equal(calls.length, 1);
});

test('warm-up phrases scored while autoAdjust is off still count toward WARMUP_PHRASES', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = false;
    mod.settings.sensitivity = 2;
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls.length, 0, 'autoAdjust was off — nothing should have been applied');
    mod.settings.autoAdjust = true;
    mod.commitPhraseResult(1.0);
    assert.equal(calls.length, 1, 'warm-up was already satisfied while paused');
});

test('Split Screen scoring state is isolated and changes only its own panel highway', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 2;
    const stateA = mod.newSplitScoreState();
    const stateB = mod.newSplitScoreState();
    let masteryA = 0.50;
    let masteryB = 0.50;
    const highwayA = { getMastery: () => masteryA, setMastery: (v) => { masteryA = v; } };
    const highwayB = { getMastery: () => masteryB, setMastery: (v) => { masteryB = v; } };

    // Each panel needs its own warm-up; A's second result must not warm B up.
    mod.commitSplitPhraseResult(stateA, highwayA, 1);
    mod.commitSplitPhraseResult(stateB, highwayB, 1);
    mod.commitSplitPhraseResult(stateA, highwayA, 1);

    assert.equal(masteryA, 0.55); // first third of the default 15-point ramp
    assert.equal(masteryB, 0.50);
});

test('qualifying streaks total the configured step for every sensitivity and direction', () => {
    for (const sensitivity of [1, 2, 3]) {
        for (const [ratio, sign] of [[1.0, 1], [0.0, -1]]) {
            const mod = freshPlugin();
            mod.settings.autoAdjust = true;
            mod.settings.sensitivity = sensitivity;
            const calls = attachHighwayStub(50);
            const th = mod.thresholds();
            for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(ratio);
            for (let i = 1; i < mod.RAMP_PHRASES; i++) mod.commitPhraseResult(ratio);
            assert.equal(calls.length, mod.RAMP_PHRASES);
            assert.equal(calls[calls.length - 1] - 50, sign * th.step,
                `sensitivity ${sensitivity}, direction ${sign > 0 ? 'up' : 'down'}`);
        }
    }
});

test('dropResistance loads true only from persisted boolean true', () => {
    const key = 'difficulty_ladder.dropResistance';
    assert.equal(freshPlugin({ stored: { [key]: JSON.stringify('false') } }).settings.dropResistance, false);
    assert.equal(freshPlugin({ stored: { [key]: JSON.stringify(true) } }).settings.dropResistance, true);
});

test('malformed dropResistance storage updates reset the setting to false', () => {
    const mod = freshPlugin();
    mod.settings.dropResistance = true;

    global.window.dispatchEvent({
        type: 'storage',
        key: 'difficulty_ladder.dropResistance',
        newValue: 'not-json',
    });

    assert.equal(mod.settings.dropResistance, false);
});

test('auto-adjust stops ramping as soon as the signal returns to neutral (no full step committed in advance)', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 2; // th.up 0.88, th.down 0.68, default reactionSpeed -> alpha 0.35
    const calls = attachHighwayStub(50);
    const th = mod.thresholds();
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls.length, 1);
    const movedSoFar = calls[0] - 50;
    assert.ok(movedSoFar < th.step, 'a single ramped step should be smaller than a full step');
    // ratio 0.4 pulls the EMA from 1.0 to 0.35*0.4 + 0.65*1.0 = 0.79 — inside
    // the neutral band (0.68, 0.88), so no further action should be taken.
    mod.commitPhraseResult(0.4);
    assert.equal(calls.length, 1, 'no new setMastery call once the EMA is back in the neutral band');
});

test('changing ramp direction restarts at the first exact-remainder increment', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 1; // exact sequence is 3, 4, 3
    mod.settings.reactionSpeed = 3; // alpha 0.5: one miss moves EMA 1.0 -> 0.5, below th.down 0.65
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls[0], 53);
    mod.commitPhraseResult(0.0); // EMA reaches the down threshold; direction flips
    assert.equal(calls[1], 50, 'downward ramp restarts with 3 rather than continuing with 4');
});

test('per-song reset clears exact-remainder ramp progress', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 1;
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls[0], 53);
    mod.resetPerSongState();
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls[1], 56, 'new song starts with 3 rather than the prior ramp\'s next 4');
});

test('drop resistance requires two consecutive below-threshold signals when enabled', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    setDropResistance(true);
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(0.0);
    assert.equal(calls.length, 0, 'first eligible low signal is held');
    setDropResistance(true); // public settings event invalidates the pending confirmation
    assert.equal(global.localStorage.getItem('difficulty_ladder.dropResistance'), 'true');
    mod.commitPhraseResult(0.0);
    assert.equal(calls.length, 0, 'first low signal after a setting event is held again');
    mod.commitPhraseResult(0.0);
    assert.equal(calls.length, 1);
    assert.equal(calls[0], 45);
});

test('manual mastery change invalidates a resisted drop before the first auto-apply', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    setDropResistance(true);
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(0.0);
    assert.equal(calls.length, 0, 'one low signal is pending');

    global.window.highway.getMastery = () => 0.60;
    mod.commitPhraseResult(0.0);
    assert.equal(calls.length, 0, 'manual value is preserved instead of applying the stale drop');
    assert.equal(mod.settings.autoAdjust, false);
});

test('drop resistance streak resets when the rolling signal returns neutral', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    setDropResistance(true);
    mod.settings.reactionSpeed = 3;
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(0.6);
    assert.equal(calls.length, 0);
    mod.commitPhraseResult(1.0); // EMA 0.8: neutral, clears the one-signal streak
    mod.commitPhraseResult(0.0); // EMA 0.4: first new low signal
    assert.equal(calls.length, 0);
    mod.commitPhraseResult(0.0); // second consecutive low signal
    assert.equal(calls.length, 1);
});

test('drop resistance does not delay upward adjustments', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    setDropResistance(true);
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls.length, 1);
    assert.equal(calls[0], 55);
});

test('min/maxMastery still clamps a ramped next value and stops repeat calls once saturated', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 2;
    mod.settings.maxMastery = 58;
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls.length, 1);
    assert.equal(calls[0], 55); // 50 + rampStep(5), under the 58 cap
    mod.commitPhraseResult(1.0); // would ramp to 60 -> clamps to 58
    assert.equal(calls.length, 2);
    assert.equal(calls[1], 58);
    mod.commitPhraseResult(1.0); // already saturated -> next === curPct -> no call
    assert.equal(calls.length, 2);
});

test('manual-override detection still disables autoAdjust after a ramped auto-apply', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 2;
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls.length, 1);
    assert.equal(mod.settings.autoAdjust, true);
    global.window.highway.getMastery = () => 0.42; // simulates a manual slider move
    mod.commitPhraseResult(1.0);
    assert.equal(mod.settings.autoAdjust, false);
    assert.equal(calls.length, 1, 'stood down instead of fighting the manual move');
});

// ── Generate ladder depth cap (generateLevels) ──────────────────────────────

test('currentTarget() clamps generateLevels to [2,8] and includes it in the /generate target', () => {
    const mod = freshPlugin();
    global.window.highway = { getSongInfo: () => ({ filename: 'song.feedpak', arrangement_index: 1 }) };
    mod.settings.generateLevels = 6;
    assert.deepEqual(mod.currentTarget(), { filename: 'song.feedpak', arrangement_index: 1, levels: 6 });
    mod.settings.generateLevels = 99; // out of range -> clamps to 8
    assert.equal(mod.currentTarget().levels, 8);
    mod.settings.generateLevels = 1; // out of range -> clamps to 2
    assert.equal(mod.currentTarget().levels, 2);
});

test('currentTarget() clamps a parsed 0 to 2 instead of falling back to the default 4', () => {
    const mod = freshPlugin();
    global.window.highway = { getSongInfo: () => ({ filename: 'song.feedpak', arrangement_index: 0 }) };
    mod.settings.generateLevels = 0; // `|| 4` would misfire here — 0 is a legitimate parse, not NaN
    assert.equal(mod.currentTarget().levels, 2);
});

test('currentTarget() returns null when there is no song loaded yet', () => {
    const mod = freshPlugin();
    global.window.highway = { getSongInfo: () => null };
    assert.equal(mod.currentTarget(), null);
});

// ── Cross-plugin contract shape parity (issue #8) ───────────────────────────
// Neither plugin defines this shape itself — it's window.highway's contract
// (feedBack core) — but both plugins' code assumes the same fields exist.
// This is a documentation-as-test guard: if either plugin's assumed field
// list drifts from the other's, this is the place a future editor would
// update both, rather than one silently going stale.
test('the phrase shape this plugin assumes matches the one sectionmap assumes (contract note)', () => {
    // getPhrases(): [{ index, start_time, end_time, max_difficulty }]
    const samplePhrase = { index: 0, start_time: 0, end_time: 10, max_difficulty: 3 };
    // Fields this plugin's drawHud()/tickScoring() reads:
    for (const key of ['start_time', 'end_time', 'max_difficulty']) {
        assert.ok(key in samplePhrase, `difficulty_ladder reads phrase.${key}`);
    }
    // Fields feedBack-plugin-sectionmap's _smSectionDifficulty/_smComputeGlass
    // read (see that repo's screen.js): the same three, plus none extra —
    // confirming neither plugin depends on a field the other doesn't also see.
    for (const key of ['start_time', 'end_time', 'max_difficulty']) {
        assert.ok(key in samplePhrase, `sectionmap reads phrase.${key}`);
    }
});

// ── Generate-difficulties CTA request path (PR #37 follow-up) ──────────────
// onGenerateClick() originally had two bugs that only threw at runtime,
// never in a type check: it called `setGenerateLabel(...)` before that
// function was defined anywhere in the file (ReferenceError, thrown
// *before* the try block, so it wasn't caught and the `finally` that
// clears `_generating` / `_generateBtn.disabled` never ran — the button
// locked up permanently after one click), and the post-success reconnect
// read a bare `hw` that was never declared in this function's scope
// (every other function does `var hw = window.highway;` locally) — a
// second ReferenceError, this one inside the try block, so it was
// swallowed and silently reported as "Generate failed" even though the
// backend generation had already succeeded. Both only manifested by
// actually invoking onGenerateClick(), which no prior test in this file
// did. `setGenerateLabel` is now a real, properly-defined helper (#41) —
// these tests still guard the underlying invariants (fetch is reached,
// reconnect fires, the button doesn't get stuck).

function fakeElement() {
    var el = {
        style: {}, title: '', id: '', disabled: false,
        classList: { toggle: function () {}, add: function () {}, remove: function () {} },
        appendChild: function () {},
        contains: function () { return false; },
        onclick: null,
    };
    return el;
}

function mountGenerateBtn(mod) {
    var created = [];
    global.document.createElement = function () {
        var el = fakeElement();
        created.push(el);
        return el;
    };
    var slot = fakeElement();
    global.window.feedBack = { uiVersion: 'v3', ui: { playerControlSlot: function () { return slot; } } };
    mod.mountControls();
    return created.filter(function (el) { return el.id === 'dynamic-difficulty-generate'; })[0];
}

test('onGenerateClick reaches fetch() without throwing on the way in (setGenerateLabel/hw setup)', async () => {
    const mod = freshPlugin();
    const btn = mountGenerateBtn(mod);
    global.window.highway = {
        getSongInfo: () => ({ filename: 'song.feedpak', arrangement_index: 0 }),
        hasPhraseData: () => false,
        reconnect: () => {},
    };
    let fetchCalled = false;
    global.fetch = async () => {
        fetchCalled = true;
        return { ok: true, json: async () => ({}) };
    };

    await mod.onGenerateClick();

    assert.equal(fetchCalled, true, 'a ReferenceError before the try block (originally: an undefined setGenerateLabel()) would prevent fetch() from ever running');
    assert.notEqual(btn.textContent, 'Generate failed');
});

test('onGenerateClick calls window.highway.reconnect() after a successful (non-skipped) generation', async () => {
    const mod = freshPlugin();
    const btn = mountGenerateBtn(mod);
    const reconnectCalls = [];
    global.window.highway = {
        getSongInfo: () => ({ filename: 'song.feedpak', arrangement_index: 2 }),
        hasPhraseData: () => false,
        reconnect: (filename, idx) => reconnectCalls.push([filename, idx]),
    };
    global.fetch = async () => ({ ok: true, json: async () => ({}) });

    await mod.onGenerateClick();

    assert.deepEqual(reconnectCalls, [['song.feedpak', 2]], 'a bare undeclared `hw` reference here used to throw and get reported as "Generate failed"');
    assert.notEqual(btn.textContent, 'Generate failed');
});

test('onGenerateClick releases the _generating guard after success, so a follow-up click is not permanently blocked', async () => {
    const mod = freshPlugin();
    mountGenerateBtn(mod);
    global.window.highway = {
        getSongInfo: () => ({ filename: 'song.feedpak', arrangement_index: 0 }),
        hasPhraseData: () => false,
        reconnect: () => {},
    };
    let fetchCallCount = 0;
    global.fetch = async () => {
        fetchCallCount++;
        return { ok: true, json: async () => ({}) };
    };

    await mod.onGenerateClick();
    await mod.onGenerateClick();

    assert.equal(fetchCallCount, 2, 'the pre-try ReferenceError used to skip the finally block, leaving _generating stuck true forever');
});
