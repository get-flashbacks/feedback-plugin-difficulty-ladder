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

test('pSuccess is 0.5 at the skill point and falls with difficulty', () => {
    assert.equal(sim.pSuccess(50, 50, 0.1), 0.5);
    assert.ok(sim.pSuccess(60, 50, 0.1) < sim.pSuccess(40, 50, 0.1));
});
