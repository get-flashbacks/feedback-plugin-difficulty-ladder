'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const sim = require('../tools/adaptive_simulation.js');
const small = { sessions: 2, phrasesPerSession: 30 };
const run = (scenario = 'plateau', config = {}) => sim.simulate({
    scenario, seed: 1234, config: { ...small, ...config },
});

test('all requested scenarios have distinct, reproducible skill trajectories', () => {
    const c = sim.configWith();
    assert.ok(sim.skillAt('fast-learner', 7, 99, c) > sim.skillAt('fast-learner', 0, 0, c));
    assert.equal(sim.skillAt('plateau', 4, 0, c), sim.skillAt('plateau', 7, 99, c));
    assert.ok(sim.skillAt('fatigue', 0, 99, c) < sim.skillAt('fatigue', 0, 0, c));
    assert.equal(sim.skillAt('fatigue', 1, 0, c), sim.skillAt('fatigue', 0, 0, c));
    assert.notEqual(sim.skillAt('day-variance', 0, 0, c, -0.3), sim.skillAt('day-variance', 0, 0, c, 0.3));
    for (const scenario of sim.SCENARIOS) assert.deepEqual(run(scenario), run(scenario));
});

test('changing seed changes outcomes without changing scenario identity', () => {
    const a = run();
    const b = sim.simulate({ scenario: 'plateau', seed: 1235, config: small });
    assert.notDeepEqual(a.rows, b.rows);
    assert.equal(a.scenario, b.scenario);
});

test('trace has discrete tiers, variable note counts, valid binomial outcomes and fixed item truth', () => {
    const r = run();
    assert.equal(r.rows.length, 60);
    assert.equal(new Set(r.rows.map(x => x.noteCount)).size, 3);
    const itemOffsets = new Map();
    for (const row of r.rows) {
        assert.equal(row.hitCount + row.missCount, row.noteCount);
        assert.equal(row.hitRate, row.hitCount / row.noteCount);
        assert.ok(Number.isInteger(row.tier) && row.tier >= 0 && row.tier <= 10);
        assert.equal(row.truth.probability, sim.sigmoid(row.truth.skill - row.truth.difficulty));
        const offset = row.truth.difficulty - 0.35 * row.tier;
        if (itemOffsets.has(row.itemId)) assert.ok(Math.abs(offset - itemOffsets.get(row.itemId)) < 1e-12);
        itemOffsets.set(row.itemId, offset);
    }
});

test('prequential EMA uses only preceding outcomes, resets each session, and does not disable', () => {
    const r = run();
    let expected = 0.5;
    for (const row of r.rows) {
        if (row.phrase === 0) expected = 0.5;
        assert.ok(Math.abs(row.prediction - expected) < 1e-12);
        expected = row.phrase === 0 ? row.hitRate : 0.35 * row.hitRate + 0.65 * expected;
    }
    // A separate VM/run cannot inherit a prior EMA or the disabled override gate.
    assert.deepEqual(run(), r);
});

test('item exploration obeys its probability extremes and reaches the full bank', () => {
    const no = run('plateau', { exploration: 0 });
    const yes = run('plateau', { exploration: 1, phrasesPerSession: 100 });
    assert.ok(no.rows.every(r => !r.exploration));
    assert.ok(yes.rows.every(r => r.exploration));
    assert.equal(new Set(no.rows.map(r => r.itemId)).size, 3);
    assert.equal(new Set(yes.rows.map(r => r.itemId)).size, 9);
});

test('oracle scoring has zero probability error; missing latent estimates stay null', () => {
    const r = run();
    const oracle = r.rows.map(row => ({ ...row, prediction: row.truth.probability }));
    const m = sim.metrics(oracle, r.config);
    assert.equal(m.probabilityRmse, 0);
    assert.equal(m.skillRmse, null);
    assert.equal(m.itemDifficultyRmse, null);
    assert.deepEqual(m.convergencePhrasesBySession, [20, 20]);
});

test('note Brier/calibration are note weighted and convergence never conflates failure with zero', () => {
    const r = run();
    const rows = [
        { ...r.rows[0], prediction: 0.5, noteCount: 1, hitCount: 1, missCount: 0, truth: { probability: 0.5 } },
        { ...r.rows[1], prediction: 0.5, noteCount: 3, hitCount: 0, missCount: 3, truth: { probability: 0.5 } },
    ];
    const m = sim.metrics(rows, r.config);
    assert.equal(m.noteBrier, 0.25);
    assert.equal(m.calibrationBias, 0.25);
    assert.deepEqual(m.convergencePhrasesBySession, [null]);
    assert.equal(sim.convergence(rows.map(x => ({ ...x, prediction: 1 })), 2, 0.1), null);
    assert.equal(sim.metrics([], r.config).probabilityRmse, null);
});

test('replay hides outcomes before prediction and never exposes truth or shares estimator mutations', () => {
    const r = run();
    const before = JSON.stringify(r);
    let calls = 0;
    sim.replay(r, () => ({
        predict(context) {
            assert.equal(context.truth, undefined);
            assert.equal(context.hitRate, undefined);
            assert.equal(context.hitCount, undefined);
            assert.equal(calls % 2, 0);
            calls++;
            context.itemId = 'mutated';
            return 0.5;
        },
        update(outcome) {
            assert.equal(calls % 2, 1);
            assert.notEqual(outcome.itemId, 'mutated');
            assert.equal(outcome.truth, undefined);
            assert.equal(outcome.hitCount + outcome.missCount, outcome.noteCount);
            calls++;
        },
    }));
    assert.equal(calls, r.rows.length * 2);
    assert.equal(JSON.stringify(r), before);
    assert.throws(() => sim.replay(r, () => ({ predict: () => NaN, update() {} })), /finite/);
});

test('invalid scenarios, empty samples, bad probabilities and malformed CLI flags fail', () => {
    assert.throws(() => run('unknown'), /scenario/);
    for (const config of [{ sessions: 0 }, { phrasesPerSession: 1.5 }, { noteCounts: [] },
        { noteCounts: [0] }, { exploration: 1.1 }, { startPct: -1 }, { sensitivity: 4 }]) {
        assert.throws(() => run('plateau', config));
    }
    assert.throws(() => sim.experiment({ seeds: 0 }));
    const result = spawnSync(process.execPath, [path.join(__dirname, '../tools/adaptive_simulation.js'), '--seeds=0'], { encoding: 'utf8' });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /seeds/);
});

test('seed aggregation and trace CLI are reproducible and label synthetic evidence', () => {
    const result = sim.experiment({ seeds: 2, config: small });
    assert.equal(result.synthetic, true);
    assert.equal(result.results.length, 4);
    for (const row of result.results) {
        assert.equal(row.runs.length, 2);
        assert.equal(row.meanProbabilityRmse, row.runs.reduce((s, r) => s + r.probabilityRmse, 0) / 2);
    }
    const cli = spawnSync(process.execPath, [path.join(__dirname, '../tools/adaptive_simulation.js'),
        '--trace=fatigue', '--sessions=1', '--phrases=2'], { encoding: 'utf8' });
    assert.equal(cli.status, 0, cli.stderr);
    const lines = cli.stdout.trim().split('\n').map(JSON.parse);
    assert.equal(lines[0].synthetic, true);
    assert.equal(lines.length, 3);
    assert.equal(lines[1].prediction, 0.5);
});
