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
// Usage: node tools/settle_points.js [--json] [--sens=1,2,3] [--react=1,2,3]
//        [--drop=0,1] [--slopes=0.05,0.1,0.2] [--notes=16] [--start=50]
//        [--down-ratio=1]   (see main() for the commands behind each #55 table)

'use strict';

let controller = null;

// Loaded once; simulate() resets per-run state via resetPerSongState(),
// which clears every field commitPhraseResult() reads or writes.
function loadController() {
    if (controller) return controller;
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
    controller = require('../screen.js');
    return controller;
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

const mean = xs => xs.reduce((s, x) => s + x, 0) / xs.length;

// Two different spreads, deliberately kept apart:
// - runMin/runMax: range of each run's mean true accuracy, i.e. how precisely
//   a setting settles.
// - p10/p90 (true accuracy) and obsP10/obsP90 (the Binomial rate the
//   controller actually sees): per-phrase swing pooled across runs. Moves and
//   reversals are driven mostly by that per-phrase noise, so they scale with
//   notes per phrase; the model has no exploration jitter of its own.
function summarize(runs) {
    const all = runs.flatMap(r => r.trueP).sort((a, b) => a - b);
    const obs = runs.flatMap(r => r.observed).sort((a, b) => a - b);
    const runMeans = runs.map(r => mean(r.trueP));
    const phrases = runs.reduce((s, r) => s + r.n, 0);
    return {
        mean: mean(all),
        runMin: Math.min(...runMeans),
        runMax: Math.max(...runMeans),
        p10: quantile(all, 0.1),
        median: quantile(all, 0.5),
        p90: quantile(all, 0.9),
        obsP10: quantile(obs, 0.1),
        obsP90: quantile(obs, 0.9),
        movesPer100: 100 * runs.reduce((s, r) => s + r.moves, 0) / phrases,
        reversalsPer100: 100 * runs.reduce((s, r) => s + r.reversals, 0) / phrases,
    };
}

// --name=a,b,c -> [a, b, c] as numbers; falls back to the default list.
function listArg(name, fallback) {
    const hit = process.argv.find(a => a.startsWith(`--${name}=`));
    if (!hit) return fallback;
    const vals = hit.slice(name.length + 3).split(',').map(Number);
    if (!vals.length || vals.some(v => !Number.isFinite(v))) {
        throw new Error(`--${name} expects comma-separated numbers`);
    }
    return vals;
}

// Reproducing the tables posted to #55:
//   main grid:        node tools/settle_points.js
//   note-count table: node tools/settle_points.js --notes=6,16,40 --react=2 --slopes=0.1 --drop=0
//   start points:     node tools/settle_points.js --start=10,30,50,70 --react=2 --drop=0
//   down-step ratio:  node tools/settle_points.js --down-ratio=1,2 --react=2 --drop=0
// (seed sets differ from the ad-hoc runs quoted in #55, so values match
// within 20-seed sampling noise rather than to the digit.)
function main() {
    const asJson = process.argv.includes('--json');
    const grid = {
        drop: listArg('drop', [0, 1]),
        sens: listArg('sens', [1, 2, 3]),
        react: listArg('react', [1, 2, 3]),
        slopes: listArg('slopes', [0.05, 0.1, 0.2]),
        notes: listArg('notes', [16]),
        start: listArg('start', [50]),
        downRatio: listArg('down-ratio', [1]),
    };
    const base = { skill: 50, phrases: 400, burnIn: 100 };
    const seeds = Array.from({ length: 20 }, (_, i) => 1000 + i);
    const rows = [];
    for (const drop of grid.drop) for (const sensitivity of grid.sens)
    for (const reactionSpeed of grid.react) for (const slope of grid.slopes)
    for (const notesPerPhrase of grid.notes) for (const startPct of grid.start)
    for (const downStepRatio of grid.downRatio) {
        const cfg = {
            ...base, dropResistance: drop === 1, sensitivity, reactionSpeed, slope,
            notesPerPhrase, startPct, downStepRatio,
        };
        const runs = seeds.map(seed => simulate({ ...cfg, seed }));
        rows.push({
            dropResistance: cfg.dropResistance, sensitivity, reactionSpeed, slope,
            notesPerPhrase, startPct, downStepRatio, ...summarize(runs),
        });
    }
    if (asJson) {
        process.stdout.write(JSON.stringify(rows, null, 2) + '\n');
        return;
    }
    const f = x => x.toFixed(2);
    console.log('drop sens react slope notes start dRatio | mean  runMin runMax | p10   p90  | obsP10 obsP90 | moves/100 rev/100');
    for (const r of rows) {
        console.log(
            `${r.dropResistance ? 'on ' : 'off'}   ${r.sensitivity}    ${r.reactionSpeed}    ${r.slope.toFixed(2)} `
            + `${String(r.notesPerPhrase).padStart(5)} ${String(r.startPct).padStart(5)} ${r.downStepRatio.toFixed(1).padStart(6)} | `
            + `${f(r.mean)}  ${f(r.runMin)}   ${f(r.runMax)}  | ${f(r.p10)}  ${f(r.p90)} | `
            + `${f(r.obsP10)}   ${f(r.obsP90)}   | `
            + `${r.movesPer100.toFixed(1).padStart(8)} ${r.reversalsPer100.toFixed(1).padStart(7)}`);
    }
}

if (require.main === module) main();

module.exports = { simulate, summarize, pSuccess, rng };
