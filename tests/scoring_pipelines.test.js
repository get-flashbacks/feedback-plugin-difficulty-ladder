'use strict';
// Characterization tests for the two scoring/tick pipelines (Stage 4-1 of
// #139, issue #163). TESTS ONLY — nothing here changes screen.js, and nothing
// here fixes or judges a behavior; it pins what the code does today so the
// unification in Stage 4-2 can be checked against it.
//
// The two pipelines under test:
//   MAIN   tickScoring -> _enqueueMainPhraseEvents / _pollMainPending
//          -> commitPhraseResult                      (the main player)
//   SPLIT  tickOneSplitHighway -> _enqueueSplitPhraseEvents /
//          _pollSplitPending -> commitSplitPhraseResult   (one per panel)
//
// HOW EACH ONE IS DRIVEN
//   * SPLIT is reached through the Node test hook: newSplitScoreState +
//     tickOneSplitHighway are exported, and its per-panel state is readable.
//   * MAIN is not exported (tickScoring is a closure), and the issue forbids
//     touching screen.js. It is therefore driven the way a browser drives it:
//     the file is evaluated in a vm context WITHOUT `module`, so the browser
//     branch runs and startRafLoops() registers tickScoring on a stubbed
//     requestAnimationFrame, which the harness then calls once per frame. The
//     only per-commit signal the main path emits is the diagnostics payload
//     (contributeDiagnostics runs exactly once per commitPhraseResult, on every
//     return path), so that is what is observed; see observe() below.
//
// Every parity scenario feeds BOTH pipelines the identical chart, verdict
// script and frame sequence, and asserts (a) they agree, and (b) the absolute
// expected outcome, so a change to either pipeline fails here, and a change to
// both in the same direction fails too.
//
// Places the pipelines DIFFER today are pinned in the "KNOWN DIFFERENCES"
// section at the bottom, each labelled DIFFERENCE, so Stage 4-2 can decide
// per item whether it is a bug or intended.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SCREEN_JS = path.join(__dirname, '..', 'screen.js');
const SCREEN_SRC = fs.readFileSync(SCREEN_JS, 'utf8');

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function playerContext(overrides = {}) {
    return {
        schema: 'difficulty_ladder.player_context.v1',
        session_id: 'session-1', player_id: 'player-1',
        profile_id: 'profile-1', profile_hash: 'hash-1',
        song_id: 'song.feedpak', arrangement_id: 'lead',
        instrument: 'guitar', role: 'lead', skill: 'overall',
        ...overrides,
    };
}

// Four 2-second phrases, a few notes and one chord in the first.
function chartA() {
    return {
        phrases: [
            { start_time: 0, end_time: 2, max_difficulty: 2 },
            { start_time: 2, end_time: 4, max_difficulty: 2 },
            { start_time: 4, end_time: 6, max_difficulty: 2 },
            { start_time: 6, end_time: 8, max_difficulty: 2 },
        ],
        notes: [
            { t: 0.5, s: 1, f: 1 }, { t: 1.0, s: 2, f: 2 }, { t: 1.5, s: 3, f: 3 },
            { t: 2.5, s: 1, f: 4 }, { t: 3.0, s: 2, f: 5 },
            { t: 4.5, s: 1, f: 6 }, { t: 5.0, s: 2, f: 7 },
            { t: 6.5, s: 1, f: 8 },
        ],
        chords: [{ t: 1.2, notes: [{ s: 0, f: 0 }, { s: 4, f: 5 }] }],
    };
}

const key = (t, s, f) => `${t}_${s}_${f}`;
const r3 = (n) => Math.round(n * 1000) / 1000;

// Frames of normal playback: playback time and wall time advance together.
function play(from, to, dt = 0.1, extra = {}) {
    const frames = [];
    for (let i = 0; from + i * dt <= to + 1e-9; i++) {
        const t = r3(from + i * dt);
        frames.push({ t, wall: t, ...extra });
    }
    return frames;
}

// A seek: playback jumps, wall time barely moves (forward jump > 1s beyond
// wall time is what _isForwardScoringDiscontinuity reads as a seek).
function seekTo(t, wallStep = 0.1, lastWall = 0) {
    return [{ t, wall: r3(lastWall + wallStep) }];
}

function emaSeq(ratios, alpha) {
    const out = [];
    let ema = null;
    for (const r of ratios) {
        ema = ema == null ? r : alpha * r + (1 - alpha) * ema;
        out.push(ema);
    }
    return out;
}

// ---------------------------------------------------------------------------
// Shared environment + highway
// ---------------------------------------------------------------------------

function makeEnv() {
    return { t: 0, wall: 0, mastery: 0.5, verdicts: new Map(), polls: 0 };
}

function makeHighway(chart, env, onSetMastery) {
    return {
        hasPhraseData: () => true,
        getPhrases: () => chart.phrases,
        getTime: () => env.t,
        getFilteredNotes: () => chart.notes,
        getFilteredChords: () => chart.chords || [],
        getMastery: () => env.mastery,
        setMastery: (fraction) => { env.mastery = fraction; if (onSetMastery) onSetMastery(fraction); },
        getNoteStateProvider: () => (note, time) => {
            env.polls++;
            return env.verdicts.get(key(time, note.s, note.f)) || null;
        },
    };
}

function applyFrame(env, frame) {
    env.t = frame.t;
    env.wall = frame.wall;
    if (frame.set) for (const [k, v] of Object.entries(frame.set)) env.verdicts.set(k, v);
    if (frame.mastery !== undefined) env.mastery = frame.mastery;
}

// ---------------------------------------------------------------------------
// SPLIT driver (Node test hook)
// ---------------------------------------------------------------------------

function installClock(env) {
    const original = Object.getOwnPropertyDescriptor(globalThis, 'performance');
    Object.defineProperty(globalThis, 'performance', {
        configurable: true, writable: true,
        value: { now: () => env.wall * 1000 },
    });
    return () => {
        if (original) Object.defineProperty(globalThis, 'performance', original);
        else delete globalThis.performance;
    };
}

function loadNodeInstance(stored = {}) {
    const store = new Map(Object.entries(stored));
    global.window = { addEventListener() {}, dispatchEvent() {} };
    global.document = { addEventListener() {}, getElementById: () => null };
    global.localStorage = {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => { store.set(k, String(v)); },
    };
    delete require.cache[require.resolve(SCREEN_JS)];
    return require(SCREEN_JS);
}

function splitDriver(chart, env, opts = {}) {
    const restoreClock = installClock(env);
    const mod = loadNodeInstance(opts.stored);
    if (opts.autoAdjust) mod.settings.autoAdjust = true;
    const masteryWrites = [];
    const hw = makeHighway(chart, env, (fraction) => masteryWrites.push(Math.round(fraction * 100)));
    const state = mod.newSplitScoreState(opts.context);
    const commits = [];
    let index = 0;
    return {
        kind: 'split', mod, state, hw, masteryWrites,
        frame(frame) {
            applyFrame(env, frame);
            const before = state.phrasesScored;
            mod.tickOneSplitHighway(hw, state);
            if (state.phrasesScored > before) {
                commits.push({ frame: index, t: frame.t, ema: state.emaHitRate });
            }
            index++;
        },
        run(frames) { frames.forEach((f) => this.frame(f)); return this; },
        observe() { return { commits: commits.map((c) => ({ ...c })), masteryWrites: [...masteryWrites] }; },
        autoAdjustEnabled: () => mod.settings.autoAdjust,
        polls: () => env.polls,
        dispose: restoreClock,
    };
}

// ---------------------------------------------------------------------------
// MAIN driver (browser branch of screen.js in a vm context)
// ---------------------------------------------------------------------------

function mainDriver(chart, env, opts = {}) {
    const diag = [];
    const raf = [];
    const masteryWrites = [];
    const store = new Map(Object.entries(opts.stored || {}));
    const player = { classList: { contains: (c) => c === 'active' }, isConnected: true };
    const sandbox = {
        console, setTimeout, clearTimeout, setInterval, clearInterval,
        addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
        performance: { now: () => env.wall * 1000 },
        requestAnimationFrame: (fn) => { raf.push(fn); return raf.length; },
        cancelAnimationFrame() {},
        document: {
            addEventListener() {},
            getElementById: (id) => (id === 'player' ? player : null),
        },
        localStorage: {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => { store.set(k, String(v)); },
        },
    };
    sandbox.window = sandbox;
    // Section Map owning the glasses makes drawHud() skip all canvas work; the
    // scoring tick is what is under test, not the HUD.
    sandbox.__slopsmithSectionMapHooksInstalled = true;
    sandbox.feedBack = {
        on: () => () => {}, off() {}, emit() {},
        diagnostics: { contribute: (id, payload) => diag.push(payload) },
    };
    sandbox.setMastery = (pct) => { masteryWrites.push(pct); env.mastery = pct / 100; };
    vm.runInNewContext(SCREEN_SRC, sandbox, { filename: SCREEN_JS });
    // Attach the highway only after load, so the synchronous tickScoring() in
    // startRafLoops() returned early and left the rAF loop armed.
    sandbox.highway = makeHighway(chart, env);
    const startCount = diag.length;
    const commits = [];
    let index = 0;
    return {
        kind: 'main', sandbox, diag, masteryWrites,
        frame(frame) {
            applyFrame(env, frame);
            const before = diag.length;
            const pending = raf.splice(0).filter((fn) => fn.name === 'tickScoring');
            assert.ok(pending.length > 0, 'the main scoring loop must still be armed');
            pending.forEach((fn) => fn());
            // contributeDiagnostics fires exactly once per committed phrase.
            if (diag.length > before) {
                assert.equal(diag.length, before + 1, 'at most one commit per frame');
                commits.push({ frame: index, t: frame.t, ema: diag[diag.length - 1].ema_hit_rate });
            }
            index++;
        },
        run(frames) { frames.forEach((f) => this.frame(f)); return this; },
        observe() { return { commits: commits.map((c) => ({ ...c })), masteryWrites: [...masteryWrites] }; },
        autoAdjustEnabled: () => (diag.length ? diag[diag.length - 1].auto_adjust_enabled : null),
        diagCountSinceLoad: () => diag.length - startCount,
        polls: () => env.polls,
        dispose() {},
    };
}

// Run the same scenario through both pipelines.
function runBoth(chart, frames, { verdicts = {}, opts = {}, stored } = {}) {
    const out = {};
    for (const make of [mainDriver, splitDriver]) {
        const env = makeEnv();
        for (const [k, v] of Object.entries(verdicts)) env.verdicts.set(k, v);
        const driver = make(chart, env, { ...opts, stored });
        try { driver.run(frames); } finally { driver.dispose(); }
        out[driver.kind] = driver;
    }
    return out;
}

function normalize(observation) {
    return {
        commits: observation.commits.map((c) => ({ frame: c.frame, t: c.t, ema: r3(c.ema) })),
        masteryWrites: observation.masteryWrites,
    };
}

function assertParity(both, expected) {
    const main = normalize(both.main.observe());
    const split = normalize(both.split.observe());
    assert.deepEqual(main, split, 'main and split pipelines must agree on this sequence');
    if (expected) assert.deepEqual(split.commits.map((c) => ({ t: c.t, ema: c.ema })), expected);
    return split;
}

const ALPHA = 0.35; // default reactionSpeed 2 -> emaAlpha()

// ---------------------------------------------------------------------------
// Harness sanity: the main path emits one diagnostics payload per commit
// ---------------------------------------------------------------------------

test('harness: the main pipeline emits exactly one diagnostics payload per committed phrase', () => {
    const verdicts = { [key(0.5, 1, 1)]: 'hit', [key(1.0, 2, 2)]: 'miss' };
    const chart = { phrases: chartA().phrases, notes: [{ t: 0.5, s: 1, f: 1 }, { t: 1.0, s: 2, f: 2 }] };
    const { main } = runBoth(chart, play(0, 1.9), { verdicts });
    assert.equal(main.diagCountSinceLoad(), 0, 'no commit while still inside the first phrase');
    main.run(play(2.0, 2.1));
    assert.equal(main.diagCountSinceLoad(), 1);
    assert.equal(main.diag[main.diag.length - 1].ema_hit_rate, 0.5, 'first commit seeds the EMA with the raw hit rate');
});

// ---------------------------------------------------------------------------
// 1. Hit/miss attribution
// ---------------------------------------------------------------------------

test('hit/miss attribution: single notes and chord notes each count once toward the phrase hit rate', () => {
    // 3 single notes + 2 chord notes = 5 judgments; hits: 0.5, 1.5, chord(0,0) = 3.
    const verdicts = {
        [key(0.5, 1, 1)]: 'hit', [key(1.0, 2, 2)]: 'miss', [key(1.5, 3, 3)]: 'hit',
        [key(1.2, 0, 0)]: 'hit', [key(1.2, 4, 5)]: 'miss',
    };
    const both = runBoth(chartA(), play(0, 2.5), { verdicts });
    assertParity(both, [{ t: 2, ema: 0.6 }]);
});

test('hit/miss attribution: each phrase is scored from its own notes and the EMA folds them in order', () => {
    const verdicts = {
        // P0: 3/5 = 0.6   P1: 1/2 = 0.5   P2: 2/2 = 1   P3: 0/1 = 0
        [key(0.5, 1, 1)]: 'hit', [key(1.0, 2, 2)]: 'miss', [key(1.5, 3, 3)]: 'hit',
        [key(1.2, 0, 0)]: 'hit', [key(1.2, 4, 5)]: 'miss',
        [key(2.5, 1, 4)]: 'hit', [key(3.0, 2, 5)]: 'miss',
        [key(4.5, 1, 6)]: 'hit', [key(5.0, 2, 7)]: 'hit',
        [key(6.5, 1, 8)]: 'miss',
    };
    const both = runBoth(chartA(), play(0, 8.5), { verdicts });
    const ema = emaSeq([0.6, 0.5, 1, 0], ALPHA).map(r3);
    assertParity(both, [
        { t: 2, ema: ema[0] }, { t: 4, ema: ema[1] }, { t: 6, ema: ema[2] }, { t: 8, ema: ema[3] },
    ]);
});

test('hit/miss attribution: a phrase with no judged notes commits nothing', () => {
    const chart = { phrases: chartA().phrases, notes: [{ t: 2.5, s: 1, f: 4 }], chords: [] };
    const verdicts = { [key(2.5, 1, 4)]: 'hit' };
    const both = runBoth(chart, play(0, 4.5), { verdicts });
    // P0 had no notes at all: crossing out of it commits nothing; P1 commits at t=4.
    assertParity(both, [{ t: 4, ema: 1 }]);
});

// ---------------------------------------------------------------------------
// 2. Phrase commit timing
// ---------------------------------------------------------------------------

test('commit timing: a phrase commits on the first frame at or after its end, not before', () => {
    const verdicts = { [key(0.5, 1, 1)]: 'hit', [key(1.0, 2, 2)]: 'hit', [key(1.5, 3, 3)]: 'hit' };
    const chart = { phrases: chartA().phrases, notes: chartA().notes.slice(0, 3) };
    const both = runBoth(chart, play(0, 2.5, 0.05), { verdicts });
    const result = assertParity(both);
    assert.equal(result.commits.length, 1);
    assert.equal(result.commits[0].t, 2, 'first frame with t >= phrase.end_time');
    // Frame index is pinned too: 0.05 steps, so t=2.0 is the 41st frame.
    assert.equal(result.commits[0].frame, 40);
});

test('commit timing: notes inside the 0.6s lookback are collected at the boundary, not dropped', () => {
    // A note at 1.9 has not reached the maturity delay when the phrase ends;
    // the boundary collects it anyway (enqueue with an infinite cutoff).
    const chart = { phrases: chartA().phrases, notes: [{ t: 0.5, s: 1, f: 1 }, { t: 1.9, s: 2, f: 2 }] };
    const verdicts = { [key(0.5, 1, 1)]: 'hit', [key(1.9, 2, 2)]: 'miss' };
    const both = runBoth(chart, play(0, 2.1), { verdicts });
    assertParity(both, [{ t: 2, ema: 0.5 }]);
});

// ---------------------------------------------------------------------------
// 3. Delayed verdicts
// ---------------------------------------------------------------------------

test('delayed verdicts: a pending note that resolves before the boundary is counted', () => {
    const chart = { phrases: chartA().phrases, notes: [{ t: 0.5, s: 1, f: 1 }, { t: 1.5, s: 3, f: 3 }] };
    const verdicts = { [key(0.5, 1, 1)]: 'active', [key(1.5, 3, 3)]: 'miss' };
    const frames = [...play(0, 1.1), { t: 1.2, wall: 1.2, set: { [key(0.5, 1, 1)]: 'hit' } }, ...play(1.3, 2.1)];
    assertParity(runBoth(chart, frames, { verdicts }), [{ t: 2, ema: 0.5 }]);
});

test('delayed verdicts: results still active at the boundary are discarded, and a late verdict never leaks forward', () => {
    const chart = {
        phrases: chartA().phrases,
        notes: [
            { t: 0.5, s: 1, f: 1 }, { t: 1.0, s: 2, f: 2 }, { t: 1.5, s: 3, f: 3 }, { t: 1.8, s: 4, f: 4 },
            { t: 2.5, s: 1, f: 9 },
        ],
    };
    const verdicts = {
        [key(0.5, 1, 1)]: 'hit', [key(1.0, 2, 2)]: 'active', [key(1.5, 3, 3)]: 'miss', [key(1.8, 4, 4)]: 'active',
        [key(2.5, 1, 9)]: 'hit',
    };
    // The 1.0 and 1.8 notes stay 'active' through the boundary, then turn into
    // hits at t=2.3 — after the phrase already committed.
    const frames = [...play(0, 2.2), { t: 2.3, wall: 2.3, set: { [key(1.0, 2, 2)]: 'hit', [key(1.8, 4, 4)]: 'hit' } }, ...play(2.4, 4.1)];
    // P0 = 1 hit of 2 resolved (0.5); the two unresolved are dropped, not
    // carried into P1. P1 = 1 hit of 1.
    const ema = emaSeq([0.5, 1], ALPHA).map(r3);
    assertParity(runBoth(chart, frames, { verdicts }), [{ t: 2, ema: ema[0] }, { t: 4, ema: ema[1] }]);
});

// ---------------------------------------------------------------------------
// 4. Forward seeks
// ---------------------------------------------------------------------------

test('forward seek: the in-flight phrase is abandoned (no commit, no fabricated judgments)', () => {
    const verdicts = { [key(0.5, 1, 1)]: 'hit', [key(4.5, 1, 6)]: 'hit', [key(5.0, 2, 7)]: 'miss' };
    const chart = { phrases: chartA().phrases, notes: [{ t: 0.5, s: 1, f: 1 }, { t: 4.5, s: 1, f: 6 }, { t: 5.0, s: 2, f: 7 }] };
    const frames = [...play(0, 1.0), ...seekTo(4.2, 0.1, 1.0), ...play(4.3, 6.5)];
    // Nothing commits at the seek (P0 abandoned, P1 skipped); P2 commits at 6.
    assertParity(runBoth(chart, frames, { verdicts }), [{ t: 6, ema: 0.5 }]);
});

test('forward seek: a large playback jump that wall time explains is NOT a seek', () => {
    // A stalled/throttled frame advances playback and wall time together, so
    // it must not be mistaken for a seek: the phrase still commits.
    const verdicts = { [key(0.5, 1, 1)]: 'hit', [key(1.0, 2, 2)]: 'hit' };
    const chart = { phrases: chartA().phrases, notes: [{ t: 0.5, s: 1, f: 1 }, { t: 1.0, s: 2, f: 2 }] };
    const frames = [...play(0, 1.5), { t: 2.5, wall: 2.5 }];
    assertParity(runBoth(chart, frames, { verdicts }), [{ t: 2.5, ema: 1 }]);
});

test('forward seek WITHIN a phrase: crossed notes are skipped and earlier judgments are dropped', () => {
    // One long phrase. Notes at 2/3/4 are crossed by the seek and must never be
    // judged (they would all read as hits); the note at 0.4 was judged before
    // the seek and must not survive it. Only the post-seek notes (both misses)
    // count, so the committed hit rate is 0, not a blend with those hits.
    const chart = {
        phrases: [{ start_time: 0, end_time: 10, max_difficulty: 2 }],
        notes: [
            { t: 0.4, s: 1, f: 1 }, { t: 2, s: 1, f: 2 }, { t: 3, s: 1, f: 3 }, { t: 4, s: 1, f: 4 },
            { t: 8.5, s: 2, f: 5 }, { t: 9, s: 2, f: 6 },
        ],
    };
    const verdicts = {
        [key(0.4, 1, 1)]: 'hit', [key(2, 1, 2)]: 'hit', [key(3, 1, 3)]: 'hit', [key(4, 1, 4)]: 'hit',
        [key(8.5, 2, 5)]: 'miss', [key(9, 2, 6)]: 'miss',
    };
    const frames = [...play(0, 1.0), ...seekTo(8.0, 0.1, 1.0), ...play(8.1, 10.2)];
    assertParity(runBoth(chart, frames, { verdicts }), [{ t: 10, ema: 0 }]);
});

test('pending judgments are re-polled at most once per 0.1s of playback, identically on both pipelines', () => {
    const chart = { phrases: [{ start_time: 0, end_time: 10, max_difficulty: 2 }], notes: [{ t: 0.5, s: 1, f: 1 }] };
    const verdicts = { [key(0.5, 1, 1)]: 'active' };
    const both = runBoth(chart, play(0.6, 3.0, 0.05), { verdicts });
    assert.equal(both.main.polls(), both.split.polls());
    // The note is enqueued at t=1.1 (cutoff t-0.6 reaches 0.5) and is then polled
    // once playback has advanced 0.1s past the previous poll. 0.05s frames over
    // 1.1..3.0 allow at most one poll per two frames (~19), and float error in
    // `t + 0.1` can stretch a gap to three frames (0.15s, ~13). Asserting the
    // band, not the exact count, keeps this about the cadence rather than about
    // how the runtime rounds; polling every frame (38) or not at all both fall outside.
    const polls = both.split.polls();
    assert.ok(polls >= 12 && polls <= 20, `expected a ~0.1s poll cadence, got ${polls} polls`);
});

test('_isForwardScoringDiscontinuity: the exact threshold (jump must exceed wall advance by MORE than 1s)', () => {
    const mod = loadNodeInstance();
    const jump = mod._isForwardScoringDiscontinuity;
    assert.equal(jump(-1, 5, 0, 1), false, 'no previous sample yet');
    assert.equal(jump(5, 10, -1, 1), false, 'no previous wall sample yet');
    assert.equal(jump(0, 2.0, 0, 1.0), false, 'jump exceeds wall advance by exactly 1s: not a seek');
    assert.equal(jump(0, 2.01, 0, 1.0), true, 'just over 1s beyond wall advance: a seek');
    assert.equal(jump(0, 3, 0, 3), false, 'playback keeping pace with wall time');
    assert.equal(jump(10, 3, 0, 1), false, 'backward movement is handled elsewhere, not here');
    // A wall clock that went backward counts as zero advance (clamped), so any
    // playback advance beyond 1s then reads as a seek.
    assert.equal(jump(0, 1.5, 5, 4), true);
    assert.equal(jump(0, 0.9, 5, 4), false);
});

// ---------------------------------------------------------------------------
// 5. Backward seeks and loops
// ---------------------------------------------------------------------------

test('backward seek within a phrase: the replay is judged fresh and never merged with the first pass', () => {
    const chart = { phrases: chartA().phrases, notes: [{ t: 0.5, s: 1, f: 1 }, { t: 1.0, s: 2, f: 2 }] };
    const verdicts = { [key(0.5, 1, 1)]: 'hit', [key(1.0, 2, 2)]: 'hit' };
    // First pass judges both as hits, then rewind; on the replay the first note
    // is a miss. Merged results would read 0.75; a fresh replay reads 0.5.
    const frames = [
        ...play(0, 1.7),
        { t: 0.2, wall: 1.8, set: { [key(0.5, 1, 1)]: 'miss' } },
        ...play(0.3, 2.1).map((f, i) => ({ ...f, wall: r3(1.9 + i * 0.1) })),
    ];
    assertParity(runBoth(chart, frames, { verdicts }), [{ t: 2, ema: 0.5 }]);
});

test('loop wrap across a phrase boundary: the rewind frame commits nothing, then the replay commits normally', () => {
    const verdicts = { [key(0.5, 1, 1)]: 'hit', [key(1.0, 2, 2)]: 'hit', [key(2.5, 1, 4)]: 'miss' };
    const chart = { phrases: chartA().phrases, notes: [{ t: 0.5, s: 1, f: 1 }, { t: 1.0, s: 2, f: 2 }, { t: 2.5, s: 1, f: 4 }] };
    const frames = [
        ...play(0, 3.0),                 // P0 commits at t=2 (hit rate 1)
        { t: 0.2, wall: 3.1 },           // loop restart while inside P1: no commit for P1
        ...play(0.3, 2.1).map((f, i) => ({ ...f, wall: r3(3.2 + i * 0.1) })),
    ];
    const ema = emaSeq([1, 1], ALPHA).map(r3);
    const result = assertParity(runBoth(chart, frames, { verdicts }));
    assert.deepEqual(result.commits.map((c) => c.t), [2, 2], 'P0 commits on each pass; the rewind itself commits nothing');
    assert.deepEqual(result.commits.map((c) => c.ema), ema);
});

// ---------------------------------------------------------------------------
// 6. Duplicate events
// ---------------------------------------------------------------------------

test('duplicate events: the same (time, string, fret) is judged once however many times it appears', () => {
    const chart = {
        phrases: chartA().phrases,
        // The note appears twice in the note list and again inside a chord at
        // the same time; (2,2) appears only in the chord. Two distinct keys.
        notes: [{ t: 0.5, s: 1, f: 1 }, { t: 0.5, s: 1, f: 1 }],
        chords: [{ t: 0.5, notes: [{ s: 1, f: 1 }, { s: 2, f: 2 }] }],
    };
    const verdicts = { [key(0.5, 1, 1)]: 'hit', [key(0.5, 2, 2)]: 'miss' };
    assertParity(runBoth(chart, play(0, 2.1), { verdicts }), [{ t: 2, ema: 0.5 }]);
});

test('duplicate events: the first terminal verdict wins; a later flip is ignored', () => {
    const chart = { phrases: chartA().phrases, notes: [{ t: 0.5, s: 1, f: 1 }, { t: 1.0, s: 2, f: 2 }] };
    const verdicts = { [key(0.5, 1, 1)]: 'hit', [key(1.0, 2, 2)]: 'hit' };
    // Both notes are judged as hits by ~t=1.7; flipping them to misses at 1.9
    // must not change the committed result.
    const frames = [...play(0, 1.8), { t: 1.9, wall: 1.9, set: { [key(0.5, 1, 1)]: 'miss', [key(1.0, 2, 2)]: 'miss' } }, ...play(2.0, 2.2)];
    assertParity(runBoth(chart, frames, { verdicts }), [{ t: 2, ema: 1 }]);
});

// ---------------------------------------------------------------------------
// 7. Split Screen per-highway state isolation
// ---------------------------------------------------------------------------

test('split isolation: panels keep fully independent score state and progress', () => {
    const envA = makeEnv();
    const envB = makeEnv();
    const restore = installClock(envA);
    try {
        const mod = loadNodeInstance();
        const chart = { phrases: chartA().phrases, notes: [{ t: 0.5, s: 1, f: 1 }, { t: 1.0, s: 2, f: 2 }] };
        const hwA = makeHighway(chart, envA);
        const hwB = makeHighway(chart, envB);
        envA.mastery = 0.5; envB.mastery = 0.8;
        envA.verdicts.set(key(0.5, 1, 1), 'hit'); envA.verdicts.set(key(1.0, 2, 2), 'hit');
        envB.verdicts.set(key(0.5, 1, 1), 'miss'); envB.verdicts.set(key(1.0, 2, 2), 'miss');
        const ctxA = playerContext({ session_id: 'split-1', player_id: 'panel-a' });
        const ctxB = playerContext({ session_id: 'split-1', player_id: 'panel-b' });
        mod.registerSplitHighway(hwA, ctxA);
        mod.registerSplitHighway(hwB, ctxB);
        const stateA = mod._splitScoreStateForHighway(hwA);
        const stateB = mod._splitScoreStateForHighway(hwB);
        assert.notEqual(stateA, stateB);
        assert.notEqual(stateA.judgedKeys, stateB.judgedKeys);
        assert.notEqual(stateA.pendingJudgments, stateB.pendingJudgments);

        // Panel A plays on; panel B has not ticked yet.
        for (const f of play(0, 2.1)) { applyFrame(envA, f); mod.tickOneSplitHighway(hwA, stateA); }
        assert.equal(stateA.phrasesScored, 1);
        assert.equal(stateA.emaHitRate, 1);
        assert.equal(stateB.phrasesScored, 0, 'B is untouched by A ticking');
        assert.equal(stateB.emaHitRate, null);
        assert.equal(stateB.judgedKeys.size, 0);

        for (const f of play(0, 2.1)) { applyFrame(envB, f); mod.tickOneSplitHighway(hwB, stateB); }
        assert.equal(stateB.emaHitRate, 0, 'B judged its own (all-miss) verdicts');
        assert.equal(stateA.emaHitRate, 1, 'A is untouched by B ticking');

        // bestMastery is written per context: difficulty * hit rate * 100.
        assert.equal(mod.readProgress(ctxA).bestMastery, 50);
        assert.equal(mod.readProgress(ctxB).bestMastery, 0);
    } finally { restore(); }
});

test('split isolation: a rewind or seek on one panel does not disturb another panel mid-phrase', () => {
    const envA = makeEnv();
    const envB = makeEnv();
    const restore = installClock(envA);
    try {
        const mod = loadNodeInstance();
        const chart = { phrases: chartA().phrases, notes: [{ t: 0.5, s: 1, f: 1 }, { t: 1.0, s: 2, f: 2 }] };
        const hwA = makeHighway(chart, envA);
        const hwB = makeHighway(chart, envB);
        for (const e of [envA, envB]) { e.verdicts.set(key(0.5, 1, 1), 'hit'); e.verdicts.set(key(1.0, 2, 2), 'hit'); }
        const stateA = mod.newSplitScoreState();
        const stateB = mod.newSplitScoreState();
        for (const f of play(0, 1.7)) { applyFrame(envA, f); mod.tickOneSplitHighway(hwA, stateA); }
        for (const f of play(0, 1.7)) { applyFrame(envB, f); mod.tickOneSplitHighway(hwB, stateB); }
        const bTotal = stateB.phraseTotal;
        assert.equal(bTotal, 2);
        applyFrame(envA, { t: 0.2, wall: 1.8 });
        mod.tickOneSplitHighway(hwA, stateA);
        assert.equal(stateA.phraseTotal, 0, 'A was rewound and reset');
        assert.equal(stateB.phraseTotal, bTotal, 'B kept its in-flight judgments');
    } finally { restore(); }
});

// ---------------------------------------------------------------------------
// KNOWN DIFFERENCES between the pipelines (pinned, not endorsed)
// ---------------------------------------------------------------------------

// Six 2-second phrases with one note each, so a perfect run commits six times.
function chartPerfect() {
    const phrases = []; const notes = []; const verdicts = {};
    for (let i = 0; i < 6; i++) {
        phrases.push({ start_time: i * 2, end_time: i * 2 + 2, max_difficulty: 2 });
        notes.push({ t: i * 2 + 0.5, s: 1, f: 1 + i });
        verdicts[key(i * 2 + 0.5, 1, 1 + i)] = 'hit';
    }
    return { chart: { phrases, notes }, verdicts };
}

test('DIFFERENCE (channel): auto-adjust applies the same ramp steps, but main writes via window.setMastery and split via highway.setMastery', () => {
    const { chart, verdicts } = chartPerfect();
    const both = runBoth(chart, play(0, 12.2), {
        verdicts, opts: { autoAdjust: true }, stored: { 'difficulty_ladder.autoAdjust': 'true' },
    });
    const main = both.main.observe();
    const split = both.split.observe();
    // The ramp itself (which percentages, in which order) is identical...
    assert.deepEqual(main.masteryWrites, split.masteryWrites);
    assert.ok(main.masteryWrites.length >= 3, 'warm-up passed, the slider ramped up');
    const mod = loadNodeInstance();
    const step = mod.rampStep(mod.thresholds(), 0, 'up');
    assert.equal(main.masteryWrites[0], 50 + step);
    // ...but the channel differs: main reports it as an auto action, split has
    // no equivalent record at all.
    const lastMain = both.main.diag[both.main.diag.length - 1];
    assert.equal(lastMain.last_auto_action.direction, 'up');
    assert.equal('lastAutoAction' in both.split.state, false);
});

test('DIFFERENCE (manual override scope): drift disables auto-adjust GLOBALLY on main, but only for that panel on split', () => {
    const { chart, verdicts } = chartPerfect();
    // Commits happen at t=2,4,6,8,10,12. The slider is moved by hand just
    // before the fifth commit (t=10), after the ramp has been acting.
    const frames = [...play(0, 9.9), { t: 10.0, wall: 10.0, mastery: 0.95 }, ...play(10.1, 12.2)];
    const both = runBoth(chart, frames, {
        verdicts, opts: { autoAdjust: true }, stored: { 'difficulty_ladder.autoAdjust': 'true' },
    });
    // Main: the plugin-wide setting flips off, and is persisted.
    assert.equal(both.main.autoAdjustEnabled(), false);
    assert.equal(both.main.sandbox.localStorage.getItem('difficulty_ladder.autoAdjust'), 'false');
    // Split: only this panel's controller stands down; the global setting stays on.
    assert.equal(both.split.state.manualOverride, true);
    assert.equal(both.split.autoAdjustEnabled(), true);
    // Neither controller acts after the drift.
    const writesAtDrift = both.main.masteryWrites.length;
    assert.equal(both.split.masteryWrites.length, writesAtDrift);
});
