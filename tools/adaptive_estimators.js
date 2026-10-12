'use strict';

const ELO_K_LOGIT = 32 * Math.LN10 / 400;

function sigmoid(x) {
    if (x >= 0) return 1 / (1 + Math.exp(-x));
    const exp = Math.exp(x);
    return exp / (1 + exp);
}

function logit(p) {
    const value = Math.max(1e-9, Math.min(1 - 1e-9, p));
    return Math.log(value / (1 - value));
}

function responseWeight(noteCount) {
    return Math.sqrt(noteCount / 16);
}

function itemState(itemBank) {
    const ids = itemBank.map(item => item.id);
    return {
        ids,
        difficulty: new Map(ids.map(id => [id, 0])),
        count: new Map(ids.map(id => [id, 0])),
    };
}

function centerItemDifficulties(state, ability) {
    const mean = state.ids.length
        ? state.ids.reduce((sum, id) => sum + state.difficulty.get(id), 0) / state.ids.length
        : 0;
    for (const id of state.ids) state.difficulty.set(id, state.difficulty.get(id) - mean);
    return ability - mean;
}

function predictWith(ability, baseDifficulty, tier, tierDifficulty) {
    const itemDifficultyEstimate = baseDifficulty + tierDifficulty * tier;
    return {
        probability: sigmoid(ability - itemDifficultyEstimate),
        skillEstimate: ability,
        itemDifficultyEstimate,
    };
}

function createElo({ itemBank, config, dynamic = false, beta = 0.06 } = {}) {
    const state = itemState(itemBank || []);
    const tierDifficulty = config && Number.isFinite(config.tierDifficulty) ? config.tierDifficulty : 0.35;
    let ability = 0;
    let playerCount = 0;

    function predict(context) {
        return predictWith(ability, state.difficulty.get(context.itemId) || 0,
            context.tier, tierDifficulty);
    }

    function update(outcome) {
        const forecast = predict(outcome);
        const error = outcome.hitRate - forecast.probability;
        const weight = responseWeight(outcome.noteCount);
        const count = state.count.get(outcome.itemId) || 0;
        const playerScale = dynamic ? 1 / (1 + beta * playerCount) : 1;
        const itemScale = dynamic ? 1 / (1 + beta * count) : 1;
        ability += ELO_K_LOGIT * playerScale * weight * error;
        state.difficulty.set(outcome.itemId,
            (state.difficulty.get(outcome.itemId) || 0) - ELO_K_LOGIT * itemScale * weight * error);
        state.count.set(outcome.itemId, count + 1);
        playerCount++;
        ability = centerItemDifficulties(state, ability);
    }

    return { predict, update, beginSession() {} };
}

function createKalmanElo({ itemBank, config, processNoise = 0.02, beta = 0.06 } = {}) {
    const state = itemState(itemBank || []);
    const tierDifficulty = config && Number.isFinite(config.tierDifficulty) ? config.tierDifficulty : 0.35;
    let ability = 0;
    let variance = 1;
    let session = null;

    function predict(context) {
        return predictWith(ability, state.difficulty.get(context.itemId) || 0,
            context.tier, tierDifficulty);
    }

    function beginSession(nextSession) {
        if (session !== null && nextSession !== session) variance += processNoise * 8;
        session = nextSession;
    }

    function update(outcome) {
        const forecast = predict(outcome);
        const oldItemDifficulty = state.difficulty.get(outcome.itemId) || 0;
        const hits = outcome.hitCount;
        const misses = outcome.missCount;
        const smoothedRate = (hits + 0.5) / (outcome.noteCount + 1);
        const observedSkill = logit(smoothedRate) + oldItemDifficulty + tierDifficulty * outcome.tier;
        const observationVariance = 1 / (hits + 0.5) + 1 / (misses + 0.5)
            + 1 / (1 + (state.count.get(outcome.itemId) || 0));
        variance += processNoise;
        const gain = variance / (variance + observationVariance);
        ability += gain * (observedSkill - ability);
        variance *= 1 - gain;

        const itemCount = state.count.get(outcome.itemId) || 0;
        const itemGain = ELO_K_LOGIT / (1 + beta * itemCount) * responseWeight(outcome.noteCount);
        state.difficulty.set(outcome.itemId, oldItemDifficulty
            - itemGain * (outcome.hitRate - forecast.probability));
        state.count.set(outcome.itemId, itemCount + 1);
        ability = centerItemDifficulties(state, ability);
    }

    return { predict, update, beginSession, uncertainty: () => variance };
}

function createLltm({ config, learningRate = 0.08 } = {}) {
    const tierDifficulty = config && Number.isFinite(config.tierDifficulty) ? config.tierDifficulty : 0.35;
    const weights = { density: 0, interval: 0 };
    let ability = 0;

    function itemDifficulty(features) {
        return weights.density * features.density + weights.interval * features.interval;
    }

    function predict(context) {
        return predictWith(ability, itemDifficulty(context.itemFeatures),
            context.tier, tierDifficulty);
    }

    function update(outcome) {
        const forecast = predict(outcome);
        const gradient = (outcome.hitCount - outcome.noteCount * forecast.probability) / 16;
        ability += learningRate * gradient;
        weights.density -= learningRate * gradient * outcome.itemFeatures.density;
        weights.interval -= learningRate * gradient * outcome.itemFeatures.interval;
    }

    return {
        predict,
        update,
        beginSession() {},
        coefficients: () => ({ ...weights }),
    };
}

const CANDIDATES = Object.freeze(['elo', 'dynamic-elo', 'kalman-elo', 'lltm']);

function createEstimator(name, options = {}) {
    if (name === 'elo') return createElo({ ...options, dynamic: false });
    if (name === 'dynamic-elo') return createElo({ ...options, dynamic: true });
    if (name === 'kalman-elo') return createKalmanElo(options);
    if (name === 'lltm') return createLltm(options);
    throw new Error(`unknown estimator: ${name}`);
}

module.exports = { CANDIDATES, ELO_K_LOGIT, sigmoid, logit, createElo, createKalmanElo, createLltm, createEstimator };
