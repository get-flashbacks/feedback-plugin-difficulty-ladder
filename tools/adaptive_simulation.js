#!/usr/bin/env node
// Phase 2 scaffold (#55, parent #31). Synthetic evidence, never player data.
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const { rng } = require('./settle_points.js');

const SCENARIOS = ['fast-learner', 'plateau', 'fatigue', 'day-variance'];
const DEFAULTS = Object.freeze({
    sessions: 8, phrasesPerSession: 100, items: 9, maxTier: 10,
    noteCounts: [6, 16, 40], exploration: 0.2,
    sensitivity: 2, reactionSpeed: 2, dropResistance: false,
    downStepRatio: 1, startPct: 50, convergenceWindow: 20,
    convergenceThreshold: 0.1,
});
const source = fs.readFileSync(path.join(__dirname, '..', 'screen.js'), 'utf8');
const controllerSha256 = crypto.createHash('sha256').update(source).digest('hex');
const sigmoid = x => 1 / (1 + Math.exp(-x));
const mean = xs => xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
const rmse = xs => xs.length ? Math.sqrt(mean(xs.map(x => x * x))) : null;
const sd = xs => xs.length ? Math.sqrt(mean(xs.map(x => (x - mean(xs)) ** 2))) : null;

function configWith(overrides = {}) {
    const c = { ...DEFAULTS, ...overrides };
    for (const key of ['sessions', 'phrasesPerSession', 'items', 'maxTier', 'convergenceWindow']) {
        if (!Number.isSafeInteger(c[key]) || c[key] < 1) throw new Error(`${key} must be a positive integer`);
    }
    if (c.items < 3) throw new Error('items must be at least 3');
    if (!Array.isArray(c.noteCounts) || !c.noteCounts.length
        || c.noteCounts.some(n => !Number.isSafeInteger(n) || n < 1)) {
        throw new Error('noteCounts must contain positive integers');
    }
    for (const key of ['exploration', 'convergenceThreshold']) {
        if (!Number.isFinite(c[key]) || c[key] < 0 || c[key] > 1) throw new Error(`${key} must be in [0,1]`);
    }
    for (const key of ['sensitivity', 'reactionSpeed']) {
        if (![1, 2, 3].includes(c[key])) throw new Error(`${key} must be 1, 2 or 3`);
    }
    if (typeof c.dropResistance !== 'boolean') throw new Error('dropResistance must be boolean');
    if (!Number.isFinite(c.downStepRatio) || c.downStepRatio <= 0) throw new Error('downStepRatio must be positive');
    if (!Number.isFinite(c.startPct) || c.startPct < 0 || c.startPct > 100) throw new Error('startPct must be in [0,100]');
    return c;
}

// Independent VM per run: no browser globals, timers, storage or scoring state
// leak into another run or into the importing process. No real player identity.
function controller(config) {
    const store = new Map();
    const sandbox = {
        module: { exports: {} }, console,
        window: { addEventListener() {}, dispatchEvent() {} },
        document: { addEventListener() {}, getElementById() { return null; } },
        localStorage: {
            getItem: k => store.get(k) ?? null,
            setItem: (k, v) => store.set(k, String(v)),
        },
        setTimeout() { return 0; }, clearTimeout() {},
        setInterval() { return 0; }, clearInterval() {},
    };
    vm.runInNewContext(source, sandbox, { filename: 'screen.js' });
    const mod = sandbox.module.exports;
    Object.assign(mod.settings, {
        autoAdjust: true, sensitivity: config.sensitivity,
        reactionSpeed: config.reactionSpeed, dropResistance: config.dropResistance,
        downStepRatio: config.downStepRatio, minMastery: 0, maxMastery: 100,
    });
    let pct = config.startPct;
    sandbox.window.highway = { getMastery: () => pct / 100 };
    sandbox.window.setMastery = next => { pct = next; };
    return {
        beginSession() { mod.resetPerSongState(); },
        predict() { return mod._mainScore().emaHitRate ?? 0.5; },
        update(rate) { mod.commitPhraseResult(rate); },
        difficulty() { return pct; },
        enabled() { return mod.settings.autoAdjust; },
    };
}

// Skill is on a fixed synthetic logit scale, not mastery percent or Elo points.
function skillAt(scenario, session, phrase, config, dayOffset = 0) {
    const t = (session * config.phrasesPerSession + phrase)
        / Math.max(1, config.sessions * config.phrasesPerSession - 1);
    const within = phrase / Math.max(1, config.phrasesPerSession - 1);
    switch (scenario) {
    case 'fast-learner': return 0.5 + 2.5 * (1 - Math.exp(-6 * t));
    case 'plateau': return 0.5 + 1.5 * Math.min(1, t / 0.25);
    case 'fatigue': return 2.5 - 1.5 * within;
    case 'day-variance': return 1.5 + 0.8 * t + dayOffset;
    default: throw new Error(`unknown scenario: ${scenario}`);
    }
}

function binomial(n, p, rand) {
    let hits = 0;
    for (let i = 0; i < n; i++) if (rand() < p) hits++;
    return hits;
}

function simulate({ scenario, seed = 1000, config = {} }) {
    const c = configWith(config);
    if (!SCENARIOS.includes(scenario)) throw new Error(`unknown scenario: ${scenario}`);
    if (!Number.isSafeInteger(seed) || seed < 0 || seed > 0xffffffff) throw new Error('seed must be uint32');
    // Separate streams: changing note count never consumes selection/day RNG.
    const select = rng(seed ^ 0x521a91), response = rng(seed ^ 0x726a82);
    const days = rng(seed ^ 0x31c490);
    const ctl = controller(c);
    const bank = Array.from({ length: c.items }, (_, i) => ({
        id: `item-${i}`, baseDifficulty: -1 + 2 * i / (c.items - 1),
    }));
    const central = bank.filter((_, i) => Math.abs(i - (c.items - 1) / 2) <= 1);
    const rows = [];
    for (let session = 0; session < c.sessions; session++) {
        ctl.beginSession(); // Production EMA resets per song; slider carries over.
        const dayOffset = (days() - 0.5) * 1.2;
        for (let phrase = 0; phrase < c.phrasesPerSession; phrase++) {
            const index = rows.length;
            const exploration = select() < c.exploration;
            // Exploration jitters ITEM selection, never the mastery slider.
            // Injecting slider jitter would trip the real manual-override gate.
            const item = exploration ? bank[Math.floor(select() * bank.length)]
                : central[index % central.length];
            const proposedPct = ctl.difficulty();
            const tier = Math.min(c.maxTier, Math.floor(proposedPct / 100 * c.maxTier));
            const notes = c.noteCounts[Math.floor(select() * c.noteCounts.length)];
            const skill = skillAt(scenario, session, phrase, c, dayOffset);
            const difficulty = item.baseDifficulty + 0.35 * tier;
            const probability = sigmoid(skill - difficulty);
            const prediction = ctl.predict(); // PRE-outcome, PRE-update.
            const hits = binomial(notes, probability, response);
            const observed = {
                session, phrase, itemId: item.id, tier, noteCount: notes,
                hitCount: hits, missCount: notes - hits, hitRate: hits / notes,
            };
            ctl.update(observed.hitRate);
            if (!ctl.enabled()) throw new Error('simulation unexpectedly disabled auto-adjust');
            rows.push({
                index, ...observed, exploration, proposedPct,
                afterPct: ctl.difficulty(), prediction,
                truth: { skill, difficulty, probability },
            });
        }
    }
    return { scenario, seed, config: c, rows };
}

// First entire trailing window within the declared probability RMSE tolerance.
// Compute per session; null means never achieved, not a zero convergence time.
function convergence(rows, window, threshold) {
    for (let i = window - 1; i < rows.length; i++) {
        const errors = rows.slice(i - window + 1, i + 1)
            .map(r => r.prediction - r.truth.probability);
        if (rmse(errors) <= threshold) return i + 1;
    }
    return null;
}

function metrics(rows, config) {
    const errors = rows.map(r => r.prediction - r.truth.probability);
    const totalNotes = rows.reduce((a, r) => a + r.noteCount, 0);
    let brier = 0, predictedHits = 0, actualHits = 0;
    const bins = Array.from({ length: 10 }, (_, bin) => ({ bin, notes: 0, predictedHits: 0, hits: 0 }));
    for (const r of rows) {
        brier += r.hitCount * (1 - r.prediction) ** 2 + r.missCount * r.prediction ** 2;
        predictedHits += r.prediction * r.noteCount;
        actualHits += r.hitCount;
        const bin = bins[Math.min(9, Math.floor(r.prediction * 10))];
        bin.notes += r.noteCount;
        bin.predictedHits += r.prediction * r.noteCount;
        bin.hits += r.hitCount;
    }
    const sessions = [...new Set(rows.map(r => r.session))];
    return {
        probabilityRmse: rmse(errors),
        // EMA estimates a rate, not anchored latent ability/item parameters.
        skillRmse: null, itemDifficultyRmse: null,
        noteBrier: totalNotes ? brier / totalNotes : null,
        calibrationBias: totalNotes ? (predictedHits - actualHits) / totalNotes : null,
        movementSdPct: sd(rows.map(r => r.afterPct - r.proposedPct)),
        movesPer100: rows.length ? 100 * rows.filter(r => r.afterPct !== r.proposedPct).length / rows.length : null,
        convergencePhrasesBySession: sessions.map(s => convergence(
            rows.filter(r => r.session === s), config.convergenceWindow, config.convergenceThreshold)),
        calibrationBins: bins.map(b => ({
            bin: b.bin, notes: b.notes,
            predictedRate: b.notes ? b.predictedHits / b.notes : null,
            observedRate: b.notes ? b.hits / b.notes : null,
        })),
    };
}

// Research adapter seam: every estimator sees the SAME EMA-selected stream.
// Truth is held back by the evaluator. This is replay, not a candidate controller
// or a claim that its own closed-loop selection policy would perform the same.
function replay(run, factory) {
    const estimator = factory();
    const rows = run.rows.map(row => {
        const context = { session: row.session, phrase: row.phrase, itemId: row.itemId,
            tier: row.tier, noteCount: row.noteCount };
        const prediction = estimator.predict({ ...context });
        if (!Number.isFinite(prediction) || prediction < 0 || prediction > 1) {
            throw new Error('estimator prediction must be finite and in [0,1]');
        }
        estimator.update({ ...context, hitCount: row.hitCount,
            missCount: row.missCount, hitRate: row.hitRate });
        return { ...row, prediction };
    });
    return metrics(rows, run.config);
}

function experiment({ seeds = 20, config = {} } = {}) {
    if (!Number.isSafeInteger(seeds) || seeds < 1 || seeds > 10000) throw new Error('seeds must be in [1,10000]');
    const c = configWith(config);
    return {
        schema: 'difficulty_ladder.adaptive_simulation.v1',
        synthetic: true, controllerSha256, config: c, seeds,
        selectionPolicy: 'shipped EMA; epsilon item exploration; discrete synthetic tiers',
        results: SCENARIOS.map(scenario => {
            const runs = Array.from({ length: seeds }, (_, i) => {
                const run = simulate({ scenario, seed: 1000 + i, config: c });
                return { seed: run.seed, ...metrics(run.rows, c) };
            });
            const values = key => runs.map(r => r[key]);
            return { scenario, meanProbabilityRmse: mean(values('probabilityRmse')),
                minProbabilityRmse: Math.min(...values('probabilityRmse')),
                maxProbabilityRmse: Math.max(...values('probabilityRmse')),
                meanNoteBrier: mean(values('noteBrier')),
                meanMovementSdPct: mean(values('movementSdPct')),
                meanMovesPer100: mean(values('movesPer100')), runs };
        }),
    };
}

function main(argv) {
    const args = new Map();
    for (const arg of argv) {
        const match = /^--([a-z-]+)=(.+)$/.exec(arg);
        if (!match || !['seeds', 'sessions', 'phrases', 'exploration', 'trace', 'seed'].includes(match[1])
            || args.has(match[1])) throw new Error(`unknown or duplicate option: ${arg}`);
        args.set(match[1], match[2]);
    }
    const config = {};
    for (const [arg, key] of [['sessions', 'sessions'], ['phrases', 'phrasesPerSession'], ['exploration', 'exploration']]) {
        if (args.has(arg)) config[key] = Number(args.get(arg));
    }
    if (args.has('trace')) {
        if (args.has('seeds')) throw new Error('--trace uses --seed, not --seeds');
        const run = simulate({ scenario: args.get('trace'), seed: Number(args.get('seed') ?? 1000), config });
        process.stdout.write(JSON.stringify({ schema: 'difficulty_ladder.adaptive_trace.v1',
            synthetic: true, controllerSha256, scenario: run.scenario, seed: run.seed, config: run.config }) + '\n');
        for (const row of run.rows) process.stdout.write(JSON.stringify(row) + '\n');
    } else {
        if (args.has('seed')) throw new Error('--seed requires --trace');
        process.stdout.write(JSON.stringify(experiment({ seeds: Number(args.get('seeds') ?? 20), config }), null, 2) + '\n');
    }
}

if (require.main === module) {
    try { main(process.argv.slice(2)); } catch (err) {
        process.stderr.write(err.message + '\n');
        process.exitCode = 1;
    }
}
module.exports = { SCENARIOS, DEFAULTS, configWith, sigmoid, skillAt,
    simulate, metrics, convergence, replay, experiment, controllerSha256 };
