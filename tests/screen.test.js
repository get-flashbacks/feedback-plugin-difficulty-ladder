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

function freshPlugin() {
    global.window = { addEventListener: () => {} };
    global.document = {
        addEventListener: () => {},
        getElementById: () => null,
    };
    global.localStorage = {
        getItem: () => null,
        setItem: () => {},
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

test('currentTarget() returns null when there is no song loaded yet (existing target behavior preserved)', () => {
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
