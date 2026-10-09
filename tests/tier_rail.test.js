'use strict';
// Issue #157 (tier rail 2/4): the standalone difficulty guide is now an
// event-driven, accessible DOM tier rail instead of a per-frame canvas glass
// HUD. These tests pin the acceptance criteria:
//   * one cell per phrase, lit segments = currentTier, outlined = topTier,
//     and NO physical-size difficulty encoding (every segment is equal-sized);
//   * an accessible name reading current + upcoming tiers;
//   * missing phrase data (and the other suppression gates) -> no rail;
//   * no requestAnimationFrame work (the render is event-driven).
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

function makeDom() {
    function el(tag) {
        const node = {
            tagName: tag,
            id: '',
            children: [],
            attributes: {},
            style: { cssText: '', display: '' },
            isConnected: true,
            appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
            removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; },
            setAttribute(k, v) { this.attributes[k] = String(v); },
            getAttribute(k) { return this.attributes[k]; },
            addEventListener() {},
            get firstChild() { return this.children[0] || null; },
        };
        return node;
    }
    const player = el('div');
    player.classList = { contains: (c) => c === 'active' };
    const document = {
        addEventListener() {},
        getElementById: (id) => (id === 'player' ? player : null),
        createElement: (tag) => el(tag),
    };
    return { document, player };
}

function freshPlugin({ dom, sectionMap = false, split = null, raf = null } = {}) {
    global.window = { addEventListener() {} };
    if (sectionMap) global.window.__slopsmithSectionMapHooksInstalled = true;
    if (split) global.window.feedBackSplitscreen = split;
    global.document = dom.document;
    global.localStorage = { getItem: () => null, setItem() {} };
    if (raf) global.requestAnimationFrame = raf;
    else delete global.requestAnimationFrame;
    const file = path.join(__dirname, '..', 'screen.js');
    delete require.cache[require.resolve(file)];
    return require(file);
}

function phrasesForRail() {
    return [
        { start_time: 0, end_time: 10, max_difficulty: 3, top_difficulty: 3 },
        { start_time: 10, end_time: 20, max_difficulty: 3, top_difficulty: 1 },
    ];
}

function railOf(player) {
    return player.children.find((c) => c.id === 'dynamic-difficulty-rail');
}

test('missing phrase data renders no rail rather than a zero-tier value', () => {
    const dom = makeDom();
    const mod = freshPlugin({ dom });
    mod.settings.showDifficultyGuide = true;
    global.window.highway = { hasPhraseData: () => false, getPhrases: () => [], getTime: () => 0 };

    assert.equal(mod._difficultyRailVisible(), false);
    mod._syncDifficultyRail();
    assert.equal(railOf(dom.player), undefined, 'no rail node is appended');
});

test('the rail is suppressed when the guide is off, Section Map owns it, or Split Screen is active', () => {
    for (const cfg of [
        { name: 'guide off', setup: (mod) => { mod.settings.showDifficultyGuide = false; } },
        { name: 'section map', sectionMap: true },
        { name: 'split active', split: { isActive: () => true } },
    ]) {
        const dom = makeDom();
        const mod = freshPlugin({ dom, sectionMap: cfg.sectionMap, split: cfg.split });
        mod.settings.showDifficultyGuide = true;
        if (cfg.setup) cfg.setup(mod);
        global.window.highway = {
            hasPhraseData: () => true, getPhrases: () => phrasesForRail(), getTime: () => 5, getMastery: () => 0.74,
        };
        mod._syncDifficultyRail();
        assert.equal(mod._difficultyRailVisible(), false, `${cfg.name}: gate closed`);
        assert.equal(railOf(dom.player), undefined, `${cfg.name}: nothing rendered`);
    }
});

test('the rail renders one cell per phrase: lit segments = currentTier, outlined = topTier', () => {
    const dom = makeDom();
    const mod = freshPlugin({ dom });
    mod.settings.showDifficultyGuide = true;
    global.window.highway = {
        hasPhraseData: () => true, getPhrases: () => phrasesForRail(), getTime: () => 5, getMastery: () => 0.74,
    };
    mod._syncDifficultyRail();

    const rail = railOf(dom.player);
    assert.ok(rail, 'rail mounted');
    assert.equal(rail.children.length, 2, 'one cell per visible phrase');

    // mastery 0.74 -> _tierFillFrac(0.74, 3).idxLevel = 2, so 3 of 4 segments lit.
    const cell0 = rail.children[0];
    assert.equal(cell0.children.length, 4, 'maxTier + 1 segments');
    const lit0 = cell0.children.filter((s) => s.style.cssText.includes('background:#e8c040'));
    assert.equal(lit0.length, 3, 'tiers 0,1,2 lit');
    assert.equal(cell0.children.filter((s) => s.style.cssText.includes('outline:')).length, 1,
        'exactly one full-detail notch');
    assert.ok(cell0.children[3].style.cssText.includes('outline:'), 'the notch sits at topTier (3)');

    // No physical-size difficulty encoding: every segment is the same size.
    const sizes = new Set(cell0.children.map((s) => s.style.cssText.match(/width:\d+px;height:\d+px/)[0]));
    assert.equal(sizes.size, 1, 'segments never change size with difficulty');
});

test('the rail carries an accessible name covering current and upcoming phrases', () => {
    const dom = makeDom();
    const mod = freshPlugin({ dom });
    mod.settings.showDifficultyGuide = true;
    global.window.highway = {
        hasPhraseData: () => true, getPhrases: () => phrasesForRail(), getTime: () => 5, getMastery: () => 0.74,
    };
    mod._syncDifficultyRail();

    const rail = railOf(dom.player);
    assert.equal(rail.getAttribute('role'), 'list');
    assert.equal(rail.getAttribute('tabindex'), '0', 'keyboard focusable');
    const label = rail.getAttribute('aria-label');
    assert.match(label, /Current phrase: tier 2 of 3, full detail at 3/);
    assert.match(label, /Upcoming phrase: tier 2 of 3, full detail at 1/);
    // The visual segments must not be read as noise.
    assert.equal(rail.children[0].getAttribute('aria-hidden'), 'true');
});

test('the rail cells allow hover so the documented title tooltip is reachable', () => {
    const dom = makeDom();
    const mod = freshPlugin({ dom });
    mod.settings.showDifficultyGuide = true;
    global.window.highway = {
        hasPhraseData: () => true, getPhrases: () => phrasesForRail(), getTime: () => 5, getMastery: () => 0.74,
    };
    mod._syncDifficultyRail();

    const rail = railOf(dom.player);
    const cell = rail.children[0];
    assert.ok(cell.style.cssText.includes('pointer-events:auto'),
        'a cell opts back into pointer events so its title fires on hover despite the pointer-inert rail');
    assert.ok(cell.getAttribute('title'), 'the cell carries the hover title');
});

test('advancing from the first to the second phrase updates the accessible name', () => {
    const dom = makeDom();
    const mod = freshPlugin({ dom });
    mod.settings.showDifficultyGuide = true;
    let t = 5;
    global.window.highway = {
        hasPhraseData: () => true, getPhrases: () => phrasesForRail(), getTime: () => t, getMastery: () => 0.74,
    };
    mod._syncDifficultyRail();
    const rail = railOf(dom.player);
    assert.match(rail.getAttribute('aria-label'), /Current phrase: tier 2 of 3, full detail at 3/);

    // `start` stays 0 for both phrases, so the signature must also carry
    // `curIdx` or the early-return leaves phrase 0 marked "Current".
    t = 15;
    mod._syncDifficultyRail();
    const label = rail.getAttribute('aria-label');
    assert.match(label, /Current phrase: tier 2 of 3, full detail at 1/,
        'the current cell tracks the phrase transition even though start is unchanged');
    assert.match(label, /Upcoming phrase: tier 2 of 3, full detail at 3/);
});

test('a host reporting no mastery shows "tier unknown" instead of fabricating tier 0', () => {
    const dom = makeDom();
    const mod = freshPlugin({ dom });
    mod.settings.showDifficultyGuide = true;
    global.window.highway = {
        hasPhraseData: () => true, getPhrases: () => phrasesForRail(), getTime: () => 5,
    };
    mod._syncDifficultyRail();

    const rail = railOf(dom.player);
    assert.match(rail.getAttribute('aria-label'), /Current phrase: tier unknown/);
    const lit = rail.children[0].children.filter((s) => s.style.cssText.includes('#e8c040'));
    assert.equal(lit.length, 0, 'nothing lit when the tier is unknown');
});

test('the rail rescans phrases only when the cached phrase stops covering the event time', () => {
    const dom = makeDom();
    const mod = freshPlugin({ dom });
    mod.settings.showDifficultyGuide = true;
    const phrases = phrasesForRail();
    let scans = 0;
    const instrumented = phrases.map((p) => p);
    Object.defineProperty(instrumented, 'findIndex', {
        value(...args) { scans++; return Array.prototype.findIndex.apply(this, args); },
    });
    global.window.highway = {
        hasPhraseData: () => true, getPhrases: () => instrumented, getTime: () => 5, getMastery: () => 0.74,
    };
    mod._syncDifficultyRail();
    assert.equal(scans, 1, 'first render has no cache to reuse');
    // A mastery event inside the same phrase: the cached index still covers
    // the event time, so no rescan.
    global.window.highway = {
        hasPhraseData: () => true, getPhrases: () => instrumented, getTime: () => 6, getMastery: () => 0.5,
    };
    mod._syncDifficultyRail();
    assert.equal(scans, 1, 'cached index reused when it still covers the event time');
    // A phrase transition invalidates the cache: one scan, and the label moves.
    global.window.highway = {
        hasPhraseData: () => true, getPhrases: () => instrumented, getTime: () => 15, getMastery: () => 0.5,
    };
    mod._syncDifficultyRail();
    assert.equal(scans, 2, 'phrase transition rescans once');
    const rail = railOf(dom.player);
    // The second phrase's rail-leading window starts at index 0 (curIdx - 1
    // clamped), so both phrases stay visible with "Current" moved to the
    // second one (mastery 0.5 on a 0..3 slider maps to tier 1).
    assert.match(rail.getAttribute('aria-label'), /Current phrase: tier \d+ of 3, full detail at 1/);
});

test('the rail render is event-driven, never a requestAnimationFrame loop', () => {
    const dom = makeDom();
    let rafCalls = 0;
    const mod = freshPlugin({ dom, raf: () => { rafCalls++; } });
    mod.settings.showDifficultyGuide = true;
    global.window.highway = {
        hasPhraseData: () => true, getPhrases: () => phrasesForRail(), getTime: () => 5, getMastery: () => 0.74,
    };
    mod._syncDifficultyRail();
    mod._syncDifficultyRail();
    assert.equal(rafCalls, 0, 'no animation-frame work drives the rail');
});

test('settings change re-syncs the rail', () => {
    const dom = makeDom();
    const mod = freshPlugin({ dom });
    mod.settings.showDifficultyGuide = true;
    global.window.highway = {
        hasPhraseData: () => true, getPhrases: () => phrasesForRail(), getTime: () => 5, getMastery: () => 0.74,
    };
    mod._syncDifficultyRail();
    assert.ok(railOf(dom.player));

    mod.settings.showDifficultyGuide = false;
    mod._applySettingsChange({ showDifficultyGuide: true });
    assert.equal(dom.player.children.some((c) => c.id === 'dynamic-difficulty-rail' && c.style.display === 'none'), true,
        'the rail hides when the guide is switched off');
});
