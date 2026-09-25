#!/usr/bin/env node
// Settle-point simulation for the auto-adjust controller (roadmap C2, #55).
//
// Drives the REAL commitPhraseResult() from screen.js (loaded with the same
// DOM-free stubs tests/screen.test.js uses) with synthetic players, so the
// numbers can't drift from the shipped controller. No human data.
//
// Player model: per-note success p(d) = 1 / (1 + exp(slope * (d - skill)))
// where d is the slider percent (0..100) and skill is the percent at which the
// player hits half the notes. A phrase's hit rate is Binomial(n, p(d)) / n.
// Deliberate simplifications (documented in the output): the chart's tier
// quantization is ignored (the slider maps straight to p), skill is constant
// within a run, and every phrase has the same note count.
//
// Usage: node tools/settle_points.js [--json]

'use strict';

const path = require('node:path');

function loadController() {
    const store = new Map();
    const listeners = new Map();
    global.window = {
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(fn);
        },
        dispatchEvent(ev) { for (const fn of listeners.get(ev.type) || []) fn(ev); },
    };
    global.document = { addEventListener: () => {}, getElementById: () => null };
    global.localStorage = {
        getItem: k => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => { store.set(k, String(v)); },
    };
    const file = path.join(__dirname, '..', 'screen.js');
    delete require.cache[require.resolve(file)];
    return require(file);
}

// Small seeded PRNG (mulberry32) so runs are reproducible.
function rng(seed) {
    let a = seed >>> 0;
    return function () {
        a = (a + 0x6D2B79F5) >>> 0;
        let t = a;
        t = Math.imul(t ^ (t >>> 15), t | 1);
        t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

function pSuccess(d, skill, slope) {
    return 1 / (1 + Math.exp(slope * (d - skill)));
}

function binomialRate(n, p, rand) {
    let hits = 0;
    for (let i = 0; i < n; i++) if (rand() < p) hits++;
    return hits / n;
}

function simulate({ sensitivity, reactionSpeed, dropResistance, downStepRatio,
                    skill, slope, notesPerPhrase, phrases, burnIn, startPct, seed }) {
    const mod = loadController();
    Object.assign(mod.settings, {
        autoAdjust: true, sensitivity, reactionSpeed, dropResistance,
        downStepRatio, minMastery: 0, maxMastery: 100,
    });
    let frac = startPct / 100;
    global.window.highway = { getMastery: () => frac };
    global.window.setMastery = pct => { frac = pct / 100; };
    mod.resetPerSongState();

    const rand = rng(seed);
    const trueP = [];
    const observed = [];
    let moves = 0;
    let reversals = 0;
    let lastDir = 0;
    for (let i = 0; i < phrases; i++) {
        const d = frac * 100;
        const p = pSuccess(d, skill, slope);
        const rate = binomialRate(notesPerPhrase, p, rand);
        mod.commitPhraseResult(rate);
        const after = frac * 100;
        if (i >= burnIn) {
            trueP.push(p);
            observed.push(rate);
            if (after !== d) {
                moves++;
                const dir = Math.sign(after - d);
                if (lastDir && dir !== lastDir) reversals++;
                lastDir = dir;
            }
        }
    }
    return { trueP, observed, moves, reversals, n: trueP.length };
}

function quantile(sorted, q) {
    const i = (sorted.length - 1) * q;
    const lo = Math.floor(i), hi = Math.ceil(i);
    const a = sorted.at(lo), b = sorted.at(hi);
    return a + ((b - a) * (i - lo));
}

function summarize(runs) {
    const all = runs.flatMap(r => r.trueP).sort((a, b) => a - b);
    const mean = all.reduce((s, x) => s + x, 0) / all.length;
    const phrases = runs.reduce((s, r) => s + r.n, 0);
    return {
        mean,
        p10: quantile(all, 0.1),
        median: quantile(all, 0.5),
        p90: quantile(all, 0.9),
        movesPer100: 100 * runs.reduce((s, r) => s + r.moves, 0) / phrases,
        reversalsPer100: 100 * runs.reduce((s, r) => s + r.reversals, 0) / phrases,
    };
}

function main() {
    const asJson = process.argv.includes('--json');
    const base = {
        downStepRatio: 1, dropResistance: false,
        skill: 50, notesPerPhrase: 16, phrases: 400, burnIn: 100, startPct: 50,
    };
    const slopes = [0.05, 0.1, 0.2];
    const seeds = Array.from({ length: 20 }, (_, i) => 1000 + i);
    const rows = [];
    for (const dropResistance of [false, true]) {
        for (const sensitivity of [1, 2, 3]) {
            for (const reactionSpeed of [1, 2, 3]) {
                for (const slope of slopes) {
                    const runs = seeds.map(seed => simulate({
                        ...base, dropResistance, sensitivity, reactionSpeed, slope, seed,
                    }));
                    rows.push({ dropResistance, sensitivity, reactionSpeed, slope, ...summarize(runs) });
                }
            }
        }
    }
    if (asJson) {
        process.stdout.write(JSON.stringify(rows, null, 2) + '\n');
        return;
    }
    const f = x => x.toFixed(2);
    console.log('dropRes sens react slope | mean  p10   med   p90  | moves/100 rev/100');
    for (const r of rows) {
        console.log(
            `${r.dropResistance ? 'on ' : 'off'}     ${r.sensitivity}    ${r.reactionSpeed}     ${r.slope.toFixed(2)}  | `
            + `${f(r.mean)}  ${f(r.p10)}  ${f(r.median)}  ${f(r.p90)} | `
            + `${r.movesPer100.toFixed(1).padStart(8)} ${r.reversalsPer100.toFixed(1).padStart(7)}`);
    }
}

if (require.main === module) main();

module.exports = { simulate, summarize, pSuccess, rng };
