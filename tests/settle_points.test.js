'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const sim = require('../tools/settle_points.js');

const base = {
    sensitivity: 2, reactionSpeed: 2, dropResistance: false, downStepRatio: 1,
    skill: 50, slope: 0.1, notesPerPhrase: 16, phrases: 120, burnIn: 20, startPct: 50,
};

test('settle-point simulation is deterministic for a fixed seed', () => {
    const a = sim.simulate({ ...base, seed: 7 });
    const b = sim.simulate({ ...base, seed: 7 });
    assert.deepEqual(a, b);
});

test('settle-point simulation drives the real controller off a too-hard start', () => {
    // Starting at the player's 50% point, the controller must step down.
    const run = sim.simulate({ ...base, seed: 11 });
    assert.ok(run.moves > 0);
    const mean = run.trueP.reduce((s, p) => s + p, 0) / run.trueP.length;
    assert.ok(mean > 0.6, `expected accuracy to rise well above the 0.5 start, got ${mean}`);
});

// Pins the simulator's calibration against the series posted to #55, so a
// change to the player model, the harness or the controller shows up here
// instead of silently changing what every reported number means.
test('default settings settle in the calibrated band posted to #55', () => {
    const cfg = { ...base, phrases: 400, burnIn: 100 };
    const runs = Array.from({ length: 20 }, (_, i) => sim.simulate({ ...cfg, seed: 1000 + i }));
    const s = sim.summarize(runs);
    assert.ok(s.mean >= 0.77 && s.mean <= 0.81, `mean ${s.mean}`);
    assert.ok(s.movesPer100 >= 5 && s.movesPer100 <= 10, `moves/100 ${s.movesPer100}`);
    assert.ok(s.runMax - s.runMin < 0.06, `per-run settle spread ${s.runMax - s.runMin}`);
});

test('pSuccess is 0.5 at the skill point and falls with difficulty', () => {
    assert.equal(sim.pSuccess(50, 50, 0.1), 0.5);
    assert.ok(sim.pSuccess(60, 50, 0.1) < sim.pSuccess(40, 50, 0.1));
});
