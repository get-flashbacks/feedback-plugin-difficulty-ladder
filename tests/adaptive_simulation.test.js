'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const sim = require('../tools/adaptive_simulation.js');
const estimators = require('../tools/adaptive_estimators.js');
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

test('synthetic item features are centered and generate hidden base difficulty', () => {
    const bank = sim.itemBank(sim.configWith());
    const average = key => bank.reduce((sum, item) => sum + item.features[key], 0) / bank.length;
    assert.ok(Math.abs(average('density')) < 1e-12);
    assert.ok(Math.abs(average('interval')) < 1e-12);
    for (const item of bank) {
        assert.equal(item.baseDifficulty, 0.55 * item.features.density + 0.45 * item.features.interval);
    }
});

test('online Rasch, uncertainty-scaled Elo, Kalman-Elo and LLTM replay without truth leakage', () => {
    const r = run('plateau', { sessions: 3, phrasesPerSession: 30 });
    for (const candidate of estimators.CANDIDATES) {
        const result = sim.replay(r, ({ itemBank, config }) => {
            assert.ok(itemBank.every(item => !Object.hasOwn(item, 'baseDifficulty')));
            const model = estimators.createEstimator(candidate, { itemBank, config });
            return {
                predict(context) {
                    for (const key of ['truth', 'baseDifficulty', 'difficulty', 'hitCount', 'hitRate']) {
                        assert.equal(context[key], undefined);
                    }
                    return model.predict(context);
                },
                update(outcome) {
                    assert.equal(outcome.truth, undefined);
                    assert.equal(outcome.hitCount + outcome.missCount, outcome.noteCount);
                    model.update(outcome);
                },
                beginSession: session => model.beginSession(session),
            };
        });
        assert.ok(Number.isFinite(result.probabilityRmse));
        assert.ok(Number.isFinite(result.skillRmse));
        assert.ok(Number.isFinite(result.itemDifficultyRmse));
        assert.ok(Number.isFinite(result.noteBrier));
    }
});

test('candidate-controlled runs share item/note draws and stabilize movement', () => {
    const config = { sessions: 2, phrasesPerSession: 30 };
    const runs = estimators.CANDIDATES.map(candidate =>
        sim.simulateControlled({ scenario: 'fatigue', candidate, seed: 4321, config }));
    for (const other of runs.slice(1)) {
        assert.deepEqual(other.rows.map(row => [row.itemId, row.noteCount, row.exploration]),
            runs[0].rows.map(row => [row.itemId, row.noteCount, row.exploration]));
    }
    for (const run of runs) {
        assert.equal(run.rows[0].tier, 5);
        assert.equal(run.rows[1].tier, 5);
        assert.ok(run.rows.every(row => Math.abs(row.tier - Math.floor(row.proposedPct / 10)) <= 1));
        assert.ok(run.rows.every(row => row.prediction >= 0 && row.prediction <= 1));
    }
});

test('candidate experiment uses disjoint tuning/evaluation seeds and reports replay and controlled metrics', () => {
    const result = sim.candidateExperiment({
        trainingSeeds: [1000], evaluationSeeds: [2000],
        config: { sessions: 1, phrasesPerSession: 30 },
    });
    assert.equal(result.synthetic, true);
    assert.equal(result.evaluationDisjointFromTuning, true);
    assert.deepEqual(result.trainingSeeds, [1000]);
    assert.deepEqual(result.evaluationSeeds, [2000]);
    assert.equal(result.results.length, 4);
    for (const scenario of result.results) {
        assert.equal(scenario.baseline.runs.length, 1);
        for (const name of estimators.CANDIDATES) {
            assert.equal(scenario.replay[name].runs.length, 1);
            assert.equal(scenario.controlled[name].runs.length, 1);
            assert.ok(Number.isFinite(scenario.controlled[name].meanSkillRmse));
            assert.equal(scenario.controlled[name].calibrationBins.length, 10);
        }
    }
    assert.ok(result.tuning['kalman-elo'].parameters.processNoise > 0);
    assert.ok(result.tuning.lltm.parameters.learningRate > 0);
    assert.throws(() => sim.candidateExperiment({
        trainingSeeds: [5], evaluationSeeds: [5], config: { sessions: 1, phrasesPerSession: 5 },
    }), /disjoint/);
});

test('comparison CLI writes reproducible output and rejects mixed modes', () => {
    const args = [path.join(__dirname, '../tools/adaptive_simulation.js'), '--mode=compare',
        '--train-seeds=1', '--eval-seeds=1', '--sessions=1', '--phrases=15'];
    const first = spawnSync(process.execPath, args, { encoding: 'utf8' });
    const second = spawnSync(process.execPath, args, { encoding: 'utf8' });
    assert.equal(first.status, 0, first.stderr);
    assert.deepEqual(JSON.parse(first.stdout), JSON.parse(second.stdout));
    const outputPath = path.join(os.tmpdir(), `adaptive-candidates-${process.pid}.json`);
    try {
        const written = spawnSync(process.execPath, [...args, `--output=${outputPath}`], { encoding: 'utf8' });
        assert.equal(written.status, 0, written.stderr);
        assert.equal(written.stdout, '');
        assert.deepEqual(JSON.parse(fs.readFileSync(outputPath, 'utf8')), JSON.parse(first.stdout));
    } finally {
        if (fs.existsSync(outputPath)) fs.unlinkSync(outputPath);
    }
    const bad = spawnSync(process.execPath, [...args, '--seeds=2'], { encoding: 'utf8' });
    assert.equal(bad.status, 1);
    assert.match(bad.stderr, /uses --train-seeds and --eval-seeds/);
});
