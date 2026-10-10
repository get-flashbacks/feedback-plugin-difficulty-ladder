'use strict';
// Issue #130: peer-surface integration tests. The README/CLAUDE compatibility
// matrix claims feature-scoped optional peers (Note Detect, Split Screen) that
// degrade gracefully when absent or older. These tests pin the *behavior*
// behind those claims, not just the prose:
//
//   - Note Detect absent            -> installing the split hook is a no-op
//   - Split Screen absent           -> wrapping happens, but no panel registers
//   - Split Screen inactive         -> no panel registers
//   - Note Detect minimum (<1.33.0) -> panels register with unpersisted,
//                                      per-highway (untagged) identities
//   - Note Detect current (>=1.33.0)-> panels register with stable
//                                      player_context identities
//   - non-ownSource detectors       -> never register
//   - destroy()                     -> releases the panel's isolated state
//   - idempotency                   -> the factory is wrapped exactly once
//
// The wrapper is only installed at module load in a real host; the CommonJS
// branch returns before that, so these tests drive
// `_installSplitScreenDetectorHook` directly (see screen.js's export).
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

function freshPlugin() {
    global.window = { addEventListener() {} };
    global.document = { addEventListener() {}, getElementById: () => null };
    global.localStorage = { getItem: () => null, setItem() {} };
    const file = path.join(__dirname, '..', 'screen.js');
    delete require.cache[require.resolve(file)];
    return require(file);
}

function withNoteDetectFactory(mod, factory) {
    global.window.createNoteDetector = factory;
    mod._installSplitScreenDetectorHook();
    return global.window.createNoteDetector;
}

test('Note Detect absent: installing the split hook is a no-op, not a crash', () => {
    const mod = freshPlugin();
    assert.equal(global.window.createNoteDetector, undefined);
    mod._installSplitScreenDetectorHook();
    assert.equal(global.window.createNoteDetector, undefined);
});

test('Split Screen absent: the factory is wrapped but no panel is registered', () => {
    const mod = freshPlugin();
    const factory = () => ({ destroy() {} });
    const wrapped = withNoteDetectFactory(mod, factory);
    assert.notEqual(wrapped, factory, 'factory should be wrapped even with Split Screen absent');

    const hw = {};
    const detector = wrapped({ ownSource: true, highway: hw });
    assert.ok(detector, 'the wrapped factory still returns the real detector');
    assert.equal(mod._splitScoreStateForHighway(hw), undefined,
        'without Split Screen active, an ownSource panel must not register for adaptive scoring');
});

test('Split Screen inactive: an ownSource panel does not register', () => {
    const mod = freshPlugin();
    global.window.feedBackSplitscreen = { isActive: () => false };
    const wrapped = withNoteDetectFactory(mod, () => ({ destroy() {} }));

    const hw = {};
    wrapped({ ownSource: true, highway: hw });
    assert.equal(mod._splitScoreStateForHighway(hw), undefined);
});

test('legacy Split Screen (slopsmithSplitscreen fallback): a panel still registers', () => {
    // The gate is `window.feedBackSplitscreen || window.slopsmithSplitscreen`
    // (screen.js:3051), keeping Split Screen v1.10.6 — which only exposes the
    // slopsmith alias — inside the documented floor. Without this case the
    // suite only exercises feedBackSplitscreen, so removing the fallback would
    // stay green while legacy panels silently stopped registering.
    const mod = freshPlugin();
    global.window.slopsmithSplitscreen = { isActive: () => true };
    const wrapped = withNoteDetectFactory(mod, () => ({ destroy() {} }));

    const hw = {};
    const context = { session_id: 's1', player_id: 'p1', profile_id: 'prof1' };
    wrapped({ ownSource: true, highway: hw, player_context: context });
    const state = mod._splitScoreStateForHighway(hw);
    assert.ok(state, 'the slopsmithSplitscreen fallback must still register a panel');
    assert.equal(state.playerKey, mod.playerContextKey(context));
});

test('non-ownSource detectors never register even when Split Screen is active', () => {
    const mod = freshPlugin();
    global.window.feedBackSplitscreen = { isActive: () => true };
    const wrapped = withNoteDetectFactory(mod, () => ({ destroy() {} }));

    const hw = {};
    wrapped({ ownSource: false, highway: hw });
    wrapped({ highway: hw });
    assert.equal(mod._splitScoreStateForHighway(hw), undefined);
});

test('Note Detect minimum (<1.33.0): a panel registers with an unpersisted, per-highway identity', () => {
    const mod = freshPlugin();
    global.window.feedBackSplitscreen = { isActive: () => true };
    const wrapped = withNoteDetectFactory(mod, () => ({ destroy() {} }));

    const hw = {};
    // No player_context: the pre-1.33.0 factory shape.
    wrapped({ ownSource: true, highway: hw });
    const state = mod._splitScoreStateForHighway(hw);
    assert.ok(state, 'the panel must still score without a player context');
    assert.equal(state.context, null, 'no stable context -> nothing to persist against');
    assert.match(state.playerKey, /^untagged-split::/, 'falls back to an in-memory identity');
});

test('Note Detect current (>=1.33.0): a panel registers with a stable, persistable identity', () => {
    const mod = freshPlugin();
    global.window.feedBackSplitscreen = { isActive: () => true };
    const wrapped = withNoteDetectFactory(mod, () => ({ destroy() {} }));

    const hw = {};
    const context = { session_id: 's1', player_id: 'p1', profile_id: 'prof1' };
    wrapped({ ownSource: true, highway: hw, player_context: context });
    const state = mod._splitScoreStateForHighway(hw);
    assert.ok(state, 'the panel registers when Split Screen is active');
    assert.ok(state.context, 'stable context is retained for persistence');
    assert.equal(state.playerKey, mod.playerContextKey(context));
    assert.doesNotMatch(state.playerKey, /^untagged-split::/);
});

test('the wrapped factory installs exactly once across repeated hook installs', () => {
    const mod = freshPlugin();
    global.window.feedBackSplitscreen = { isActive: () => true };
    const wrapped = withNoteDetectFactory(mod, () => ({ destroy() {} }));
    mod._installSplitScreenDetectorHook();
    mod._installSplitScreenDetectorHook();
    assert.equal(global.window.createNoteDetector, wrapped, 'no second wrap');
    assert.equal(global.window.createNoteDetector.__ddSplitWrapped, true);
});

test('destroy() releases the registered panel state', () => {
    const mod = freshPlugin();
    global.window.feedBackSplitscreen = { isActive: () => true };
    let destroyed = false;
    const wrapped = withNoteDetectFactory(mod, () => ({ destroy() { destroyed = true; } }));

    const hw = {};
    const det = wrapped({ ownSource: true, highway: hw, player_context: { session_id: 's1', player_id: 'p1', profile_id: 'p1' } });
    assert.ok(mod._splitScoreStateForHighway(hw));

    det.destroy();
    assert.equal(destroyed, true, 'the real destroy still runs');
    assert.equal(mod._splitScoreStateForHighway(hw), undefined, 'isolated score state is released');
});
