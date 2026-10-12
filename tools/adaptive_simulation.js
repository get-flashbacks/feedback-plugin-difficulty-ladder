#!/usr/bin/env node
// Phase 2 scaffold (#55, parent #31). Synthetic evidence, never player data.
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const { rng } = require('./settle_points.js');
const estimatorModels = require('./adaptive_estimators.js');

const SCENARIOS = ['fast-learner', 'plateau', 'fatigue', 'day-variance'];
const DEFAULTS = Object.freeze({
    sessions: 8, phrasesPerSession: 100, items: 9, maxTier: 10,
    noteCounts: [6, 16, 40], exploration: 0.2,
    sensitivity: 2, reactionSpeed: 2, dropResistance: false,
    downStepRatio: 1, startPct: 50, convergenceWindow: 20,
    convergenceThreshold: 0.1, tierDifficulty: 0.35, targetSuccessRate: 0.75,
});
const source = fs.readFileSync(path.join(__dirname, '..', 'screen.js'), 'utf8');
const controllerSha256 = crypto.createHash('sha256').update(source).digest('hex');
const sigmoid = x => 1 / (1 + Math.exp(-x));
const mean = xs => xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
const rmse = xs => xs.length ? Math.sqrt(mean(xs.map(x => x * x))) : null;
const sd = xs => xs.length ? Math.sqrt(mean(xs.map(x => (x - mean(xs)) ** 2))) : null;

function positiveIntegers(config, keys) {
    for (const key of keys) {
        if (!Number.isSafeInteger(config[key]) || config[key] < 1) {
            throw new Error(`${key} must be a positive integer`);
        }
    }
}

function finiteRange(value, key, min, max) {
    if (!Number.isFinite(value) || value < min || value > max) {
        throw new Error(`${key} must be in [${min},${max}]`);
    }
}

function validateNoteCounts(counts) {
    if (!Array.isArray(counts) || !counts.length
        || counts.some(n => !Number.isSafeInteger(n) || n < 1)) {
        throw new Error('noteCounts must contain positive integers');
    }
}

function configWith(overrides = {}) {
    const c = { ...DEFAULTS, ...overrides };
    positiveIntegers(c, ['sessions', 'phrasesPerSession', 'items', 'maxTier', 'convergenceWindow']);
    if (c.items < 3) throw new Error('items must be at least 3');
    validateNoteCounts(c.noteCounts);
    for (const key of ['exploration', 'convergenceThreshold']) {
        finiteRange(c[key], key, 0, 1);
    }
    for (const key of ['sensitivity', 'reactionSpeed']) {
        if (![1, 2, 3].includes(c[key])) throw new Error(`${key} must be 1, 2 or 3`);
    }
    if (typeof c.dropResistance !== 'boolean') throw new Error('dropResistance must be boolean');
    if (!Number.isFinite(c.downStepRatio) || c.downStepRatio <= 0) throw new Error('downStepRatio must be positive');
    finiteRange(c.startPct, 'startPct', 0, 100);
    finiteRange(c.tierDifficulty, 'tierDifficulty', 0, 5);
    finiteRange(c.targetSuccessRate, 'targetSuccessRate', 0.01, 0.99);
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

function itemBank(config) {
    const raw = Array.from({ length: config.items }, (_, i) => ({
        id: `item-${i}`, density: i % 3 - 1, interval: Math.floor(i / 3),
    }));
    const means = {
        density: mean(raw.map(item => item.density)),
        interval: mean(raw.map(item => item.interval)),
    };
    return raw.map(item => {
        const features = {
            density: item.density - means.density,
            interval: item.interval - means.interval,
        };
        return { id: item.id, features,
            baseDifficulty: 0.55 * features.density + 0.45 * features.interval };
    });
}

function estimatorItemBank(bank) {
    return bank.map(item => ({ id: item.id, features: { ...item.features } }));
}

function estimatorConfig(config) {
    return { tierDifficulty: config.tierDifficulty };
}

function simulate({ scenario, seed = 1000, config = {} }) {
    const c = configWith(config);
    if (!SCENARIOS.includes(scenario)) throw new Error(`unknown scenario: ${scenario}`);
    if (!Number.isSafeInteger(seed) || seed < 0 || seed > 0xffffffff) throw new Error('seed must be uint32');
    // Separate streams: changing note count never consumes selection/day RNG.
    const select = rng(seed ^ 0x521a91), response = rng(seed ^ 0x726a82);
    const days = rng(seed ^ 0x31c490);
    const ctl = controller(c);
    const bank = itemBank(c);
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
            const difficulty = item.baseDifficulty + c.tierDifficulty * tier;
            const probability = sigmoid(skill - difficulty);
            const prediction = ctl.predict(); // PRE-outcome, PRE-update.
            const hits = binomial(notes, probability, response);
            const observed = {
                session, phrase, itemId: item.id, itemFeatures: { ...item.features }, tier, noteCount: notes,
                hitCount: hits, missCount: notes - hits, hitRate: hits / notes,
            };
            ctl.update(observed.hitRate);
            if (!ctl.enabled()) throw new Error('simulation unexpectedly disabled auto-adjust');
            rows.push({
                index, ...observed, exploration, proposedPct,
                afterPct: ctl.difficulty(), prediction,
                truth: { skill, difficulty, baseDifficulty: item.baseDifficulty, probability },
            });
        }
    }
    return { scenario, seed, config: c, itemBank: bank, rows };
}

function selectTier(estimator, context, maxTier, targetRate, currentTier) {
    let selected = currentTier, bestError = Infinity;
    for (let tier = 0; tier <= maxTier; tier++) {
        const result = estimator.predict({ ...context, tier });
        const probability = typeof result === 'number' ? result : result.probability;
        if (!Number.isFinite(probability) || probability < 0 || probability > 1) {
            throw new Error('estimator prediction must be finite and in [0,1]');
        }
        const error = Math.abs(probability - targetRate);
        if (error < bestError - 1e-12
            || (Math.abs(error - bestError) <= 1e-12
                && Math.abs(tier - currentTier) < Math.abs(selected - currentTier))) {
            selected = tier;
            bestError = error;
        }
    }
    return selected;
}

function advanceTier(policy, targetTier, phrase) {
    const direction = Math.sign(targetTier - policy.currentTier);
    if (phrase < 2 || direction === 0) {
        policy.moveDirection = 0;
        policy.confirmCount = 0;
        return;
    }
    if (direction === policy.moveDirection) policy.confirmCount++;
    else { policy.moveDirection = direction; policy.confirmCount = 1; }
    if (policy.confirmCount >= 2 && phrase - policy.lastMovePhrase >= 3) {
        policy.currentTier += policy.moveDirection;
        policy.lastMovePhrase = phrase;
        policy.moveDirection = 0;
        policy.confirmCount = 0;
    }
}

function simulateControlled({ scenario, candidate, seed = 2000, config = {}, parameters = {} }) {
    const c = configWith(config);
    if (!SCENARIOS.includes(scenario)) throw new Error(`unknown scenario: ${scenario}`);
    if (!Number.isSafeInteger(seed) || seed < 0 || seed > 0xffffffff) throw new Error('seed must be uint32');
    if (!estimatorModels.CANDIDATES.includes(candidate)) throw new Error(`unknown estimator: ${candidate}`);
    const select = rng(seed ^ 0x521a91), response = rng(seed ^ 0x726a82);
    const days = rng(seed ^ 0x31c490);
    const bank = itemBank(c);
    const central = bank.filter((_, i) => Math.abs(i - (c.items - 1) / 2) <= 1);
    const estimator = estimatorModels.createEstimator(candidate, {
        itemBank: estimatorItemBank(bank), config: estimatorConfig(c), ...parameters,
    });
    const rows = [];
    const policy = {
        currentTier: Math.min(c.maxTier, Math.floor(c.startPct / 100 * c.maxTier)),
        moveDirection: 0, confirmCount: 0, lastMovePhrase: -Infinity,
    };
    for (let session = 0; session < c.sessions; session++) {
        if (typeof estimator.beginSession === 'function') estimator.beginSession(session);
        policy.moveDirection = 0;
        policy.confirmCount = 0;
        policy.lastMovePhrase = -Infinity;
        const dayOffset = (days() - 0.5) * 1.2;
        for (let phrase = 0; phrase < c.phrasesPerSession; phrase++) {
            const index = rows.length;
            const exploration = select() < c.exploration;
            const item = exploration ? bank[Math.floor(select() * bank.length)]
                : central[index % central.length];
            const notes = c.noteCounts[Math.floor(select() * c.noteCounts.length)];
            const common = { session, phrase, itemId: item.id,
                itemFeatures: { ...item.features }, noteCount: notes };
            const desiredTier = selectTier(estimator, common, c.maxTier, c.targetSuccessRate, policy.currentTier);
            const proposedPct = policy.currentTier / c.maxTier * 100;
            advanceTier(policy, desiredTier, phrase);
            const tier = policy.currentTier;
            const prediction = estimator.predict({ ...common, tier });
            const forecast = typeof prediction === 'number' ? { probability: prediction } : prediction;
            const skill = skillAt(scenario, session, phrase, c, dayOffset);
            const difficulty = item.baseDifficulty + c.tierDifficulty * tier;
            const probability = sigmoid(skill - difficulty);
            const hits = binomial(notes, probability, response);
            const observed = {
                session, phrase, itemId: item.id, itemFeatures: { ...item.features }, tier,
                noteCount: notes, hitCount: hits, missCount: notes - hits, hitRate: hits / notes,
            };
            estimator.update({ ...common, ...observed, tier });
            rows.push({
                index, ...observed, exploration, proposedPct,
                afterPct: tier / c.maxTier * 100,
                prediction: forecast.probability,
                skillEstimate: Number.isFinite(forecast.skillEstimate) ? forecast.skillEstimate : null,
                itemDifficultyEstimate: Number.isFinite(forecast.itemDifficultyEstimate)
                    ? forecast.itemDifficultyEstimate : null,
                truth: { skill, difficulty, baseDifficulty: item.baseDifficulty, probability },
            });
        }
    }
    return { scenario, candidate, seed, config: c, itemBank: bank, rows };
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
    const skillErrors = rows.filter(r => Number.isFinite(r.skillEstimate))
        .map(r => r.skillEstimate - r.truth.skill);
    const itemDifficultyErrors = rows.filter(r => Number.isFinite(r.itemDifficultyEstimate))
        .map(r => r.itemDifficultyEstimate - r.truth.difficulty);
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
    const movements = rows.map(r => r.afterPct - r.proposedPct);
    const movementMean = mean(movements);
    return {
        probabilityRmse: rmse(errors),
        skillRmse: rmse(skillErrors), itemDifficultyRmse: rmse(itemDifficultyErrors),
        noteBrier: totalNotes ? brier / totalNotes : null,
        calibrationBias: totalNotes ? (predictedHits - actualHits) / totalNotes : null,
        movementSdPct: sd(movements),
        movementVariancePct2: mean(movements.map(value => (value - movementMean) ** 2)),
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
    const estimator = factory({
        itemBank: estimatorItemBank(run.itemBank || []),
        config: estimatorConfig(run.config),
    });
    let lastSession = null;
    const rows = run.rows.map(row => {
        if (row.session !== lastSession && typeof estimator.beginSession === 'function') {
            estimator.beginSession(row.session);
        }
        lastSession = row.session;
        const context = { session: row.session, phrase: row.phrase, itemId: row.itemId,
            itemFeatures: row.itemFeatures ? { ...row.itemFeatures } : undefined,
            tier: row.tier, noteCount: row.noteCount };
        const result = estimator.predict({ ...context,
            itemFeatures: context.itemFeatures ? { ...context.itemFeatures } : undefined });
        const prediction = typeof result === 'number' ? result : result && result.probability;
        if (!Number.isFinite(prediction) || prediction < 0 || prediction > 1) {
            throw new Error('estimator prediction must be finite and in [0,1]');
        }
        estimator.update({ ...context,
            itemFeatures: context.itemFeatures ? { ...context.itemFeatures } : undefined,
            hitCount: row.hitCount, missCount: row.missCount, hitRate: row.hitRate });
        return {
            ...row, prediction,
            skillEstimate: result && Number.isFinite(result.skillEstimate) ? result.skillEstimate : null,
            itemDifficultyEstimate: result && Number.isFinite(result.itemDifficultyEstimate)
                ? result.itemDifficultyEstimate : null,
        };
    });
    return metrics(rows, run.config);
}

function experiment({ seeds = 20, config = {} } = {}) {
    if (!Number.isSafeInteger(seeds) || seeds < 1 || seeds > 10000) throw new Error('seeds must be in [1,10000]');
    const c = configWith(config);
    return {
        schema: 'difficulty_ladder.adaptive_simulation.v1',
        synthetic: true, controllerSha256,
        simulatorSha256: crypto.createHash('sha256').update(fs.readFileSync(__filename)).digest('hex'),
        config: c, seeds,
        selectionPolicy: 'shipped EMA; epsilon item exploration; discrete synthetic tiers',
        itemDifficulty: 'centered synthetic density/interval features; true base difficulty = 0.55*density + 0.45*interval',
        anchor: 'mean base-item difficulty fixed at zero; known tier effect is 0.35 logit per tier',
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

function seedRange(start, count) {
    if (!Number.isSafeInteger(count) || count < 1 || count > 10000) {
        throw new Error('seed count must be in [1,10000]');
    }
    return Array.from({ length: count }, (_, i) => start + i);
}

function summarizeRuns(runs) {
    const fields = ['probabilityRmse', 'skillRmse', 'itemDifficultyRmse', 'noteBrier',
        'calibrationBias', 'movementSdPct', 'movementVariancePct2', 'movesPer100'];
    const runFields = [...fields, 'convergencePhrasesBySession'];
    const summary = { runs: runs.map(run => {
        const scalar = { seed: run.seed };
        for (const field of runFields) scalar[field] = run[field];
        return scalar;
    }) };
    for (const field of fields) {
        const values = runs.map(run => run[field]).filter(Number.isFinite);
        summary[`mean${field[0].toUpperCase()}${field.slice(1)}`] = mean(values);
    }
    const errors = runs.map(run => run.probabilityRmse).filter(Number.isFinite);
    summary.minProbabilityRmse = errors.length ? Math.min(...errors) : null;
    summary.maxProbabilityRmse = errors.length ? Math.max(...errors) : null;
    const convergenceValues = runs.flatMap(run => run.convergencePhrasesBySession || []);
    const converged = convergenceValues.filter(Number.isFinite);
    summary.convergenceSessions = convergenceValues.length;
    summary.convergedSessions = converged.length;
    summary.meanConvergencePhrases = mean(converged);
    const bins = Array.from({ length: 10 }, (_, bin) => ({ bin, notes: 0, predictedHits: 0, hits: 0 }));
    for (const run of runs) {
        for (const sourceBin of run.calibrationBins || []) {
            const target = bins[sourceBin.bin];
            target.notes += sourceBin.notes;
            if (sourceBin.notes) {
                target.predictedHits += sourceBin.predictedRate * sourceBin.notes;
                target.hits += sourceBin.observedRate * sourceBin.notes;
            }
        }
    }
    summary.calibrationBins = bins.map(bin => ({
        bin: bin.bin, notes: bin.notes,
        predictedRate: bin.notes ? bin.predictedHits / bin.notes : null,
        observedRate: bin.notes ? bin.hits / bin.notes : null,
    }));
    return summary;
}

function validateSeedSets(trainingSeeds, evaluationSeeds) {
    if (!Array.isArray(trainingSeeds) || !trainingSeeds.length
        || !Array.isArray(evaluationSeeds) || !evaluationSeeds.length) {
        throw new Error('trainingSeeds and evaluationSeeds must be non-empty arrays');
    }
    const allSeeds = [...trainingSeeds, ...evaluationSeeds];
    if (allSeeds.some(seed => !Number.isSafeInteger(seed) || seed < 0 || seed > 0xffffffff)) {
        throw new Error('all seeds must be uint32 integers');
    }
    if (new Set(allSeeds).size !== allSeeds.length) throw new Error('training and evaluation seeds must be disjoint');
}

function tuneCandidates(trainingRuns) {
    const tuning = {};
    const parameterGrids = {
        'kalman-elo': { processNoise: [0.002, 0.01, 0.02, 0.05, 0.1] },
        lltm: { learningRate: [0.16, 0.24, 0.3, 0.4, 0.5, 0.6] },
    };
    for (const candidate of estimatorModels.CANDIDATES) {
        const grid = parameterGrids[candidate];
        if (!grid) {
            tuning[candidate] = { parameters: {}, trainingProbabilityRmse: null,
                selectedOn: 'fixed from literature/design' };
            continue;
        }
        const [parameter, values] = Object.entries(grid)[0];
        const trials = values.map(value => {
            const parameters = { [parameter]: value };
            const errors = trainingRuns.map(run => replay(run, ({ itemBank: items, config: runConfig }) =>
                estimatorModels.createEstimator(candidate, {
                    itemBank: items, config: runConfig, ...parameters,
                })
            ).probabilityRmse);
            return { parameters, meanProbabilityRmse: mean(errors) };
        }).sort((a, b) => a.meanProbabilityRmse - b.meanProbabilityRmse);
        tuning[candidate] = {
            parameters: trials[0].parameters,
            trainingProbabilityRmse: trials[0].meanProbabilityRmse,
            selectedOn: 'prequential replay only; training seeds; pooled scenarios',
            trials,
        };
    }
    const parameters = Object.fromEntries(estimatorModels.CANDIDATES
        .map(name => [name, tuning[name].parameters]));
    return { tuning, parameters };
}

function evaluateCandidateScenario(scenario, seeds, config, parametersByCandidate) {
    const baselineRuns = [];
    const replayRuns = Object.fromEntries(estimatorModels.CANDIDATES.map(name => [name, []]));
    const controlledRuns = Object.fromEntries(estimatorModels.CANDIDATES.map(name => [name, []]));
    for (const seed of seeds) {
        const baseline = simulate({ scenario, seed, config });
        baselineRuns.push({ seed, ...metrics(baseline.rows, config) });
        for (const name of estimatorModels.CANDIDATES) {
            const parameters = parametersByCandidate[name];
            const replayMetrics = replay(baseline, ({ itemBank: items, config: runConfig }) =>
                estimatorModels.createEstimator(name, {
                    itemBank: items, config: runConfig, ...parameters,
                })
            );
            replayRuns[name].push({ seed, ...replayMetrics });
            const controlled = simulateControlled({ scenario, candidate: name, seed, config, parameters });
            controlledRuns[name].push({ seed, ...metrics(controlled.rows, config) });
        }
    }
    return {
        scenario,
        baseline: summarizeRuns(baselineRuns),
        replay: Object.fromEntries(estimatorModels.CANDIDATES.map(name => [name, summarizeRuns(replayRuns[name])])),
        controlled: Object.fromEntries(estimatorModels.CANDIDATES.map(name => [name, summarizeRuns(controlledRuns[name])])),
    };
}

function candidateExperiment({
    trainingSeeds = seedRange(1000, 20),
    evaluationSeeds = seedRange(2000, 20),
    config = {},
} = {}) {
    validateSeedSets(trainingSeeds, evaluationSeeds);
    const c = configWith(config);
    const trainingRuns = SCENARIOS.flatMap(scenario => trainingSeeds.map(seed =>
        simulate({ scenario, seed, config: c })));
    const { tuning, parameters } = tuneCandidates(trainingRuns);
    const results = SCENARIOS.map(scenario =>
        evaluateCandidateScenario(scenario, evaluationSeeds, c, parameters));
    return {
        schema: 'difficulty_ladder.adaptive_candidates.v1',
        synthetic: true,
        controllerSha256,
        simulatorSha256: crypto.createHash('sha256').update(fs.readFileSync(__filename)).digest('hex'),
        estimatorSha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(__dirname, 'adaptive_estimators.js'))).digest('hex'),
        config: c,
        trainingSeeds,
        evaluationSeeds,
        evaluationDisjointFromTuning: true,
        selection: {
            targetSuccessRate: c.targetSuccessRate,
            exploration: c.exploration,
            tierMovesPerAction: 1,
            confirmationPhrases: 2,
            cooldownPhrases: 3,
            warmupPhrases: 2,
            itemFeatures: 'centered synthetic density and interval features; true base difficulty = 0.55*density + 0.45*interval',
            anchor: 'mean base-item difficulty is fixed at zero; tier effect is known actuator slope, not estimated',
            evaluation: 'prequential replay on paired EMA stream plus estimator-controlled tier selection with shared item/note/response RNG streams',
        },
        tuning,
        results,
    };
}

function parseArgs(argv) {
    const allowed = ['mode', 'output', 'seeds', 'train-seeds', 'eval-seeds', 'sessions', 'phrases', 'exploration', 'trace', 'seed'];
    const args = new Map();
    for (const arg of argv) {
        const match = /^--([a-z-]+)=(.+)$/.exec(arg);
        if (!match || !allowed.includes(match[1]) || args.has(match[1])) {
            throw new Error(`unknown or duplicate option: ${arg}`);
        }
        args.set(match[1], match[2]);
    }
    return args;
}

function cliConfig(args) {
    const config = {};
    for (const [arg, key] of [['sessions', 'sessions'], ['phrases', 'phrasesPerSession'], ['exploration', 'exploration']]) {
        if (args.has(arg)) config[key] = Number(args.get(arg));
    }
    return config;
}

function writeOutput(args, text) {
    if (args.has('output')) fs.writeFileSync(path.resolve(args.get('output')), text);
    else process.stdout.write(text);
}

function runComparison(args, config) {
    if (args.has('trace') || args.has('seed') || args.has('seeds')) {
        throw new Error('--mode=compare uses --train-seeds and --eval-seeds');
    }
    const result = candidateExperiment({
        trainingSeeds: seedRange(1000, Number(args.get('train-seeds') ?? 20)),
        evaluationSeeds: seedRange(2000, Number(args.get('eval-seeds') ?? 20)),
        config,
    });
    writeOutput(args, JSON.stringify(result, null, 2) + '\n');
}

function runTrace(args, config) {
    if (args.get('mode') !== undefined && args.get('mode') !== 'baseline'
        || args.has('seeds') || args.has('train-seeds') || args.has('eval-seeds')) {
        throw new Error('--trace uses --seed and cannot be combined with seed-count or compare options');
    }
    const run = simulate({ scenario: args.get('trace'), seed: Number(args.get('seed') ?? 1000), config });
    const lines = [JSON.stringify({ schema: 'difficulty_ladder.adaptive_trace.v1',
        synthetic: true, controllerSha256, scenario: run.scenario, seed: run.seed, config: run.config })];
    for (const row of run.rows) lines.push(JSON.stringify(row));
    writeOutput(args, lines.join('\n') + '\n');
}

function runBaseline(args, config) {
    if (args.has('seed') || args.has('train-seeds') || args.has('eval-seeds')) {
        throw new Error('--seed requires --trace; train/eval seed counts require --mode=compare');
    }
    const result = experiment({ seeds: Number(args.get('seeds') ?? 20), config });
    writeOutput(args, JSON.stringify(result, null, 2) + '\n');
}

function main(argv) {
    const args = parseArgs(argv);
    const config = cliConfig(args);
    const mode = args.get('mode') ?? 'baseline';
    if (mode === 'compare') return runComparison(args, config);
    if (args.has('trace')) return runTrace(args, config);
    if (mode !== 'baseline') throw new Error(`unknown mode: ${mode}`);
    return runBaseline(args, config);
}

if (require.main === module) {
    try { main(process.argv.slice(2)); } catch (err) {
        process.stderr.write(err.message + '\n');
        process.exitCode = 1;
    }
}
module.exports = { SCENARIOS, DEFAULTS, configWith, sigmoid, skillAt, itemBank,
    simulate, simulateControlled, metrics, convergence, replay, experiment, candidateExperiment, seedRange,
    controllerSha256 };
