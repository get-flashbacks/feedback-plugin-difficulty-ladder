'use strict';
// Coverage for pure/DOM-light helpers in screen.js: sensitivity thresholds,
// per-song key derivation, judgment-dedup key shape.
// Runs under the org reusable CI as `node tests/screen.test.js` (mirrors the
// convention in feedBack-plugin-sectionmap's tests/screen.test.js).
//
// This file also documents (issue #8) the phrase-data contract this plugin
// shares with feedBack-plugin-sectionmap: both plugins independently read
// window.highway.getPhrases() / hasPhraseData() / getMastery() — see
// COMPLIANCE.md. The shape-parity test below guards that contract from
// silently drifting on either side.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

function freshPlugin({ stored = {}, onSet = null } = {}) {
    const listeners = new Map();
    const store = new Map(Object.entries(stored));
    global.window = {
        addEventListener(type, listener) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(listener);
        },
        dispatchEvent(event) {
            for (const listener of listeners.get(event.type) || []) listener(event);
        },
    };
    global.document = {
        addEventListener: () => {},
        getElementById: () => null,
    };
    global.localStorage = {
        getItem: (key) => store.has(key) ? store.get(key) : null,
        setItem: (key, value) => {
            store.set(key, String(value));
            if (onSet) onSet(key, String(value));
        },
    };
    const file = path.join(__dirname, '..', 'screen.js');
    delete require.cache[require.resolve(file)];
    return require(file);
}

test('thresholds() at sensitivity 2 (default) matches the documented up/down/step values', () => {
    const mod = freshPlugin();
    mod.settings.sensitivity = 2;
    assert.deepEqual(mod.thresholds(), { up: 0.88, down: 0.68, step: 15 });
});

function assertThresholdsClose(actual, expected) {
    assert.ok(Math.abs(actual.up - expected.up) < 1e-9, `up: ${actual.up} ~= ${expected.up}`);
    assert.ok(Math.abs(actual.down - expected.down) < 1e-9, `down: ${actual.down} ~= ${expected.down}`);
    assert.equal(actual.step, expected.step);
}

test('thresholds() clamps sensitivity to [1,3]', () => {
    const mod = freshPlugin();
    mod.settings.sensitivity = 1;
    assertThresholdsClose(mod.thresholds(), { up: 0.93, down: 0.65, step: 10 });
    mod.settings.sensitivity = 3;
    assertThresholdsClose(mod.thresholds(), { up: 0.83, down: 0.71, step: 20 });
    mod.settings.sensitivity = 99; // out of range -> clamps to 3's values
    assertThresholdsClose(mod.thresholds(), { up: 0.83, down: 0.71, step: 20 });
});

test('songKeyOf combines filename and arrangement_index so per-song state never leaks across arrangements', () => {
    const mod = freshPlugin();
    assert.equal(mod.songKeyOf({ filename: 'song.feedpak', arrangement_index: 0 }), 'song.feedpak::0');
    assert.equal(mod.songKeyOf({ filename: 'song.feedpak', arrangement_index: 1 }), 'song.feedpak::1');
});

test('songKeyOf falls back to the arrangement name when arrangement_index is absent', () => {
    const mod = freshPlugin();
    assert.equal(mod.songKeyOf({ filename: 'song.feedpak', arrangement: 'Bass' }), 'song.feedpak::Bass');
});

test('songKeyOf returns null for no song info (no song loaded yet)', () => {
    const mod = freshPlugin();
    assert.equal(mod.songKeyOf(null), null);
    assert.equal(mod.songKeyOf(undefined), null);
});

test('currentTargetStatus distinguishes missing highway API from no loaded song', () => {
    const mod = freshPlugin();
    assert.deepEqual(mod.currentTargetStatus(), { ok: false, reason: 'unavailable' });

    global.window.highway = { getSongInfo: () => null };
    assert.deepEqual(mod.currentTargetStatus(), { ok: false, reason: 'unloaded' });
});

test('currentTargetStatus returns the generate target when a song is loaded', () => {
    const mod = freshPlugin();
    mod.settings.generateLevels = 99;
    global.window.highway = {
        getSongInfo: () => ({ filename: 'song.feedpak', arrangement_index: 2 }),
    };

    assert.deepEqual(mod.currentTargetStatus(), {
        ok: true,
        target: { filename: 'song.feedpak', arrangement_index: 2, levels: 8 },
    });
});

test('publishes the Section Map compatibility capability after the plugin rename', () => {
    freshPlugin();
    assert.equal(global.window._ddCapabilities.sectionDifficulty, true);
});

test('currentTargetStatus uses feedBack.currentSong filename with the real highway song-info shape', () => {
    const mod = freshPlugin();
    global.window.feedBack = {
        currentSong: { filename: 'library/song.feedpak', arrangementIndex: 3 },
    };
    global.window.highway = {
        // Core's song_info deliberately has no filename.
        getSongInfo: () => ({ title: 'Song', arrangement_index: 3 }),
    };

    assert.deepEqual(mod.currentTargetStatus(), {
        ok: true,
        target: { filename: 'library/song.feedpak', arrangement_index: 3, levels: 4 },
    });
});

test('judgmentKey is stable and distinct per (time, string, fret)', () => {
    const mod = freshPlugin();
    assert.equal(mod.judgmentKey(1.5, 2, 3), '1.5_2_3');
    assert.notEqual(mod.judgmentKey(1.5, 2, 3), mod.judgmentKey(1.5, 3, 2));
});

// ── Per-phrase attempt logging (issue #53) ─────────────────────────────────

test('_phraseIdOf includes the song arrangement, phrase index, and stable time window', () => {
    const mod = freshPlugin();
    assert.equal(
        mod._phraseIdOf('song.feedpak::0', 2, { start_time: 12.34567, end_time: 15 }),
        'song.feedpak::0::2::12.346::15.000'
    );
});

test('_presentedDifficultyLevel maps current mastery onto the phrase ladder level', () => {
    const mod = freshPlugin();
    assert.equal(mod._presentedDifficultyLevel({ getMastery: () => 0.0 }, { max_difficulty: 3 }), 0);
    assert.equal(mod._presentedDifficultyLevel({ getMastery: () => 0.74 }, { max_difficulty: 3 }), 2);
    assert.equal(mod._presentedDifficultyLevel({ getMastery: () => 1.0 }, { max_difficulty: 3 }), 3);
    assert.equal(mod._presentedDifficultyLevel({ getMastery: () => 0.5 }, { max_difficulty: 0 }), 0);
    assert.equal(mod._presentedDifficultyLevel(null, { max_difficulty: 3 }), null);
    assert.equal(mod._presentedDifficultyLevel({}, { max_difficulty: 3 }), null);
    assert.equal(mod._presentedDifficultyLevel({ getMastery: () => 0.5 }, { max_difficulty: NaN }), 0);
    assert.equal(mod._presentedDifficultyLevel({ getMastery: () => 0.5 }, { max_difficulty: 'not-a-number' }), 0);
    assert.equal(mod._presentedDifficultyLevel({ getMastery: () => NaN }, { max_difficulty: 3 }), null);
    assert.equal(mod._presentedDifficultyLevel({ getMastery: () => 'not-a-number' }, { max_difficulty: 3 }), null);
});

// ── Issue #63: one discrete tier formula shared by every glass renderer ────

test('_tierFillFrac matches the documented discrete tier ladder', () => {
    const mod = freshPlugin();
    assert.deepEqual(mod._tierFillFrac(0.0, 3), { idxLevel: 0, fillFrac: 0 });
    assert.deepEqual(mod._tierFillFrac(0.74, 3), { idxLevel: 2, fillFrac: 2 / 3 });
    assert.deepEqual(mod._tierFillFrac(1.0, 3), { idxLevel: 3, fillFrac: 1 });
});

test('_tierFillFrac reports fully filled when there is no tier ladder to climb', () => {
    const mod = freshPlugin();
    assert.deepEqual(mod._tierFillFrac(0.5, 0), { idxLevel: 0, fillFrac: 1 });
    assert.deepEqual(mod._tierFillFrac(0.5, NaN), { idxLevel: 0, fillFrac: 1 });
    assert.deepEqual(mod._tierFillFrac(0.5, -1), { idxLevel: 0, fillFrac: 1 });
});

test('_tierFillFrac clamps out-of-range mastery instead of over/under-filling', () => {
    const mod = freshPlugin();
    assert.deepEqual(mod._tierFillFrac(-1, 3), { idxLevel: 0, fillFrac: 0 });
    assert.deepEqual(mod._tierFillFrac(2, 3), { idxLevel: 3, fillFrac: 1 });
});

function stubHighwayForSectionDifficulty({ sections, phrases, mastery }) {
    return {
        getSections: () => sections,
        getPhrases: () => phrases,
        getMastery: () => mastery,
    };
}

test('calculateAndEmitSectionDifficulties fills each section using the same discrete tier drawHud() uses', () => {
    const mod = freshPlugin();
    // A section whose only overlapping phrase caps at difficulty 2 (of a
    // song-wide max of 4) is a real case the old continuous formula got
    // wrong: mastery 0.6 against max_difficulty 2 lands on tier 1 (of 2),
    // i.e. 50% -- not the 30% the old `mastery * maxSectionDifficulty /
    // globalMaxDifficulty` formula would have emitted.
    global.window.highway = stubHighwayForSectionDifficulty({
        sections: [{ time: 0, name: 'Verse' }, { time: 10, name: 'Chorus' }],
        phrases: [
            { start_time: 0, end_time: 10, max_difficulty: 2 },
            { start_time: 10, end_time: 20, max_difficulty: 4 },
        ],
        mastery: 0.6,
    });
    let emitted = null;
    global.window.feedBack = { emit: (name, detail) => { emitted = { name, detail }; } };

    mod.calculateAndEmitSectionDifficulties();

    assert.equal(emitted.name, 'difficulty:sections-updated');
    const verse = emitted.detail.sectionDifficulties[0];
    const expected = mod._tierFillFrac(0.6, 2);
    assert.equal(verse.maxDifficulty, 2);
    assert.equal(verse.fillPercentage, expected.fillFrac * 100);
    assert.equal(verse.fillPercentage, 50); // tier 1 of 2, not the old 30%
});

test('calculateAndEmitSectionDifficulties reports 0% for a section whose only phrase has no difficulty ladder, even if other phrases in the song do', () => {
    // Regression (Sourcery, PR #79): _tierFillFrac(mastery, 0) returns
    // fillFrac 1 ("fully filled") for drawHud's per-phrase "no ladder"
    // convention, but at the section level maxSectionDifficulty === 0 means
    // "no difficulty content overlaps this section at all" (e.g. an empty
    // section next to phrases that do have depth) -- reusing the per-phrase
    // convention here would render an empty section as misleadingly "fully
    // mastered". The old continuous formula always emitted 0% for this case.
    const mod = freshPlugin();
    global.window.highway = stubHighwayForSectionDifficulty({
        sections: [{ time: 0, name: 'Intro' }, { time: 5, name: 'Verse' }],
        phrases: [
            { start_time: 0, end_time: 5, max_difficulty: 0 },
            { start_time: 5, end_time: 20, max_difficulty: 4 },
        ],
        mastery: 0.6,
    });
    let emitted = null;
    global.window.feedBack = { emit: (name, detail) => { emitted = { name, detail }; } };

    mod.calculateAndEmitSectionDifficulties();

    const intro = emitted.detail.sectionDifficulties[0];
    assert.equal(intro.maxDifficulty, 0);
    assert.equal(intro.fillPercentage, 0);
});

test('phrase attempt log helpers ignore malformed storage and retain an array shape', () => {
    const key = 'difficulty_ladder.phraseAttempts.v1';
    const mod = freshPlugin({ stored: { [key]: '{"not":"an array"}' } });

    assert.deepEqual(mod.loadPhraseAttempts(), []);
    mod.savePhraseAttempts([{ phrase_id: 'p1' }]);
    assert.deepEqual(mod.loadPhraseAttempts(), [{ phrase_id: 'p1' }]);
});

// ── Player-context persistence v2 (#82/#87/#88) ───────────────────────────

function playerContext(overrides = {}) {
    return {
        schema: 'difficulty_ladder.player_context.v1',
        session_id: 'session-1',
        player_id: 'player-1',
        profile_id: 'profile-1',
        profile_hash: 'hash-1',
        song_id: 'song.feedpak',
        arrangement_id: 'lead',
        instrument: 'guitar',
        role: 'lead',
        skill: 'overall',
        ...overrides,
    };
}

test('progress v2 keeps current difficulty distinct from best mastery', () => {
    const mod = freshPlugin();
    const ctx = playerContext();

    assert.equal(mod.writeProgress(ctx, { currentDifficulty: 63, bestMastery: 48 }), true);
    assert.deepEqual(
        { currentDifficulty: mod.readProgress(ctx).currentDifficulty, bestMastery: mod.readProgress(ctx).bestMastery },
        { currentDifficulty: 63, bestMastery: 48 }
    );

    mod.writeProgress(ctx, { currentDifficulty: 70 });
    assert.equal(mod.readProgress(ctx).bestMastery, 48, 'difficulty writes must not overwrite long-term mastery');
    mod.writeProgress(ctx, { bestMastery: 12 });
    assert.equal(mod.readProgress(ctx).bestMastery, 48, 'best mastery is monotonic');
});

test('an explicit null currentDifficulty is not coerced into a false 0%', () => {
    const mod = freshPlugin();
    const ctx = playerContext();

    // Number(null) === 0, so a naive numeric coercion would persist this as
    // a real 0% difficulty instead of leaving it unset.
    mod.writeProgress(ctx, { currentDifficulty: null, bestMastery: 55 });
    assert.equal(mod.readProgress(ctx).currentDifficulty, null);
    assert.equal(mod.readProgress(ctx).bestMastery, 55);
});

test('players sharing one profile keep independent progress and phrase-attempt records', () => {
    const mod = freshPlugin();
    const playerA = playerContext({ player_id: 'player-a' });
    const playerB = playerContext({ player_id: 'player-b' });

    assert.notEqual(mod.persistenceContextKey(playerA), mod.persistenceContextKey(playerB));
    mod.writeProgress(playerA, { currentDifficulty: 35 });
    mod.writeProgress(playerB, { currentDifficulty: 82 });
    mod.savePhraseAttempts([{ phrase_id: 'a-only' }], playerA);
    mod.savePhraseAttempts([{ phrase_id: 'b-only' }], playerB);

    assert.equal(mod.readProgress(playerA).currentDifficulty, 35);
    assert.equal(mod.readProgress(playerB).currentDifficulty, 82);
    assert.deepEqual(mod.loadPhraseAttempts(playerA).map(x => x.phrase_id), ['a-only']);
    assert.deepEqual(mod.loadPhraseAttempts(playerB).map(x => x.phrase_id), ['b-only']);

    const progress = mod.loadProgressStore();
    assert.deepEqual(
        Object.keys(progress.profiles[mod._nodeKey('hash-1')].players).sort(),
        [mod._nodeKey('player-a'), mod._nodeKey('player-b')].sort(),
    );
});

test('library badge scan ignores malformed role nodes in scoped progress', () => {
    const mod = freshPlugin();
    const main = playerContext({ player_id: 'main' });
    mod.upsertPlayerContext(main);
    mod.writeProgress(main, { currentDifficulty: 45 });
    const store = mod.loadProgressStore();
    store.profiles[mod._nodeKey('hash-1')].players[mod._nodeKey('main')].songs[mod._nodeKey('song.feedpak')]
        .arrangements[mod._nodeKey('lead')].instruments[mod._nodeKey('guitar')].roles[mod._nodeKey('lead')] = null;
    mod.saveProgressStore(store);

    assert.doesNotThrow(() => mod._dominantSongMastery({ filename: 'song.feedpak' }));
    assert.equal(mod._dominantSongMastery({ filename: 'song.feedpak' }), null);
});

test('four simultaneous player contexts remain independently addressable', () => {
    const mod = freshPlugin();
    for (let i = 1; i <= 4; i++) {
        const ctx = playerContext({ player_id: `player-${i}`, profile_hash: `hash-${i}` });
        mod.upsertPlayerContext(ctx);
        mod.writeProgress(ctx, { currentDifficulty: i * 10 });
    }

    assert.equal(mod.listPlayerContexts().length, 4);
    for (let i = 1; i <= 4; i++) {
        const ctx = playerContext({ player_id: `player-${i}`, profile_hash: `hash-${i}` });
        assert.equal(mod.readProgress(ctx).currentDifficulty, i * 10);
    }
});

test('profiles do not share progress for the same player/song/part', () => {
    const mod = freshPlugin();
    const a = playerContext({ profile_id: 'alice', profile_hash: 'alice-hash' });
    const b = playerContext({ profile_id: 'bob', profile_hash: 'bob-hash' });
    mod.writeProgress(a, { currentDifficulty: 35 });
    mod.writeProgress(b, { currentDifficulty: 85 });
    assert.equal(mod.readProgress(a).currentDifficulty, 35);
    assert.equal(mod.readProgress(b).currentDifficulty, 85);
});

test('instrument and role dimensions isolate saved difficulty', () => {
    const mod = freshPlugin();
    const guitar = playerContext({ instrument: 'guitar', role: 'lead' });
    const bass = playerContext({ instrument: 'bass', role: 'bass' });
    const karaoke = playerContext({ instrument: 'voice', role: 'karaoke' });
    mod.writeProgress(guitar, { currentDifficulty: 81 });
    mod.writeProgress(bass, { currentDifficulty: 52 });
    mod.writeProgress(karaoke, { currentDifficulty: 67 });
    assert.equal(mod.readProgress(guitar).currentDifficulty, 81);
    assert.equal(mod.readProgress(bass).currentDifficulty, 52);
    assert.equal(mod.readProgress(karaoke).currentDifficulty, 67);
});

test('karaoke normalization uses voice without introducing fretted defaults', () => {
    const mod = freshPlugin();
    const ctx = mod.normalizePlayerContext(playerContext({ instrument: '', role: 'vocals' }));
    assert.equal(ctx.instrument, 'voice');
    assert.equal(ctx.role, 'karaoke');
});

test('skill-specific progress is isolated and falls back to overall only when absent', () => {
    const mod = freshPlugin();
    const overall = playerContext({ skill: 'overall' });
    const pinch = playerContext({ skill: 'pinch-harmonics' });
    const bends = playerContext({ skill: 'bends' });
    mod.writeProgress(overall, { currentDifficulty: 40 });
    assert.equal(mod.readProgress(pinch).currentDifficulty, 40, 'missing skill reads overall');
    mod.writeProgress(pinch, { currentDifficulty: 75 });
    assert.equal(mod.readProgress(pinch).currentDifficulty, 75);
    assert.equal(mod.readProgress(bends).currentDifficulty, 40);
    assert.equal(mod.readProgress(overall).currentDifficulty, 40, 'skill write does not replace overall');
});

test('player-scoped difficulty dispatch carries the complete context and persists the target', () => {
    const mod = freshPlugin();
    const requests = [];
    const emitted = [];
    global.window.feedBack = {
        capabilities: {
            dispatch: (name, payload) => { requests.push({ name, payload }); return true; },
        },
        emit: (name, detail) => emitted.push({ name, detail }),
    };
    const ctx = playerContext({ session_id: 'split-42', player_id: 'player-4' });

    assert.equal(mod._applyDifficultyForContext(ctx, 67, null, 'adaptive'), true);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].name, 'player-difficulty.v1');
    assert.equal(requests[0].payload.schema, 'difficulty_ladder.difficulty_request.v1');
    assert.equal(requests[0].payload.current_difficulty, 67);
    assert.deepEqual(requests[0].payload.player_context, mod._contextEventPayload(ctx));
    assert.equal(mod.readProgress(ctx).currentDifficulty, 67);
    assert.equal(emitted[0].name, 'difficulty:player-changed');
    assert.equal(emitted[0].detail.player_context.player_id, 'player-4');
});

test('a non-true capability result falls back to the context-owned highway', () => {
    const mod = freshPlugin();
    const ctx = playerContext({ player_id: 'player-fallback' });
    let mastery = 0.4;
    global.window.feedBack = {
        capabilities: { dispatch: () => undefined },
        emit: () => {},
    };
    const highway = {
        setMastery: value => { mastery = value; },
        getMastery: () => mastery,
    };

    assert.equal(mod._applyDifficultyForContext(ctx, 73, highway, 'adaptive'), true);
    assert.equal(mastery, 0.73);
    assert.equal(mod.readProgress(ctx).currentDifficulty, 73);
});

test('section difficulty events retain the player context for pane-local rendering', () => {
    const mod = freshPlugin();
    const ctx = playerContext({ session_id: 'split-42', player_id: 'player-3' });
    const emitted = [];
    global.window.feedBack = { emit: (name, detail) => emitted.push({ name, detail }) };
    const highway = stubHighwayForSectionDifficulty({
        sections: [{ time: 0, name: 'Verse' }],
        phrases: [{ start_time: 0, end_time: 10, max_difficulty: 3 }],
        mastery: 0.5,
    });

    mod.calculateAndEmitSectionDifficulties(ctx, highway);

    assert.equal(emitted[0].name, 'difficulty:sections-updated');
    assert.deepEqual(emitted[0].detail.player_context, mod._contextEventPayload(ctx));
});

test('split phrase finalization records the completed phrase under its own player context', () => {
    const mod = freshPlugin();
    const ctx = playerContext({ session_id: 'split-42', player_id: 'player-2' });
    let time = 0.8;
    const highway = {
        hasPhraseData: () => true,
        getPhrases: () => [
            { start_time: 0, end_time: 1, max_difficulty: 2 },
            { start_time: 1, end_time: 2, max_difficulty: 2 },
        ],
        getTime: () => time,
        getNoteStateProvider: () => () => 'hit',
        getFilteredNotes: () => [{ t: 0.1, s: 1, f: 2 }],
        getFilteredChords: () => [],
        getMastery: () => 0.5,
    };
    const state = mod.newSplitScoreState(ctx);

    mod.tickOneSplitHighway(highway, state);
    time = 1.2;
    mod.tickOneSplitHighway(highway, state);

    const attempts = mod.loadPhraseAttempts(ctx);
    assert.equal(attempts.length, 1);
    assert.equal(attempts[0].player_id, 'player-2');
    assert.equal(attempts[0].session_id, 'split-42');
    assert.equal(attempts[0].instrument, 'guitar');
    assert.equal(attempts[0].skill, 'overall');
});

test('a backward seek within the same phrase re-judges notes instead of reusing stale judgments', () => {
    const mod = freshPlugin();
    const ctx = playerContext({ session_id: 'split-seek', player_id: 'player-seek' });
    // Notes are only judged once they've fallen 0.6s behind the lookback
    // window (see tickOneSplitHighway's `cutoff = t - 0.6`), matching the
    // 0.8s starting time the existing split-phrase-finalization test above
    // uses for the same reason.
    let time = 0.8;
    let judgment = 'hit';
    const highway = {
        hasPhraseData: () => true,
        getPhrases: () => [{ start_time: 0, end_time: 1, max_difficulty: 2 }],
        getTime: () => time,
        getNoteStateProvider: () => () => judgment,
        getFilteredNotes: () => [{ t: 0.1, s: 1, f: 2 }],
        getFilteredChords: () => [],
        getMastery: () => 0.5,
    };
    const state = mod.newSplitScoreState(ctx);

    mod.tickOneSplitHighway(highway, state);
    assert.equal(state.phraseHits, 1);
    assert.equal(state.phraseTotal, 1);

    // Seek backward within the same phrase (loop/rewind), then replay the
    // same note but this time it's missed. Without resetting judgedKeys on
    // the seek, the note's key is still marked judged from the first pass
    // and this replay is silently skipped, leaving the stale hit in place.
    time = 0.05;
    judgment = 'miss';
    mod.tickOneSplitHighway(highway, state);
    time = 0.8;
    mod.tickOneSplitHighway(highway, state);
    assert.equal(state.phraseTotal, 1, 'the replay must be judged fresh, not merged with the first pass');
    assert.equal(state.phraseHits, 0, 'the replay missed — the stale hit must not survive the seek');
});

test('a pending judgment can resolve after aging beyond the old two-second window', () => {
    const mod = freshPlugin();
    let time = 0.8;
    let judgment = 'active';
    let providerPolls = 0;
    let frames = 0;
    const highway = {
        hasPhraseData: () => true,
        getPhrases: () => [{ start_time: 0, end_time: 10, max_difficulty: 2 }],
        getTime: () => time,
        getNoteStateProvider: () => () => { providerPolls++; return judgment; },
        getFilteredNotes: () => [{ t: 0.1, s: 1, f: 2 }],
        getFilteredChords: () => [],
        getMastery: () => 0.5,
    };
    const state = mod.newSplitScoreState();

    mod.tickOneSplitHighway(highway, state);
    frames++;
    assert.equal(state.pendingJudgments.size, 1);
    assert.equal(state.phraseTotal, 0);

    for (let next = 0.85; next <= 3.1; next += 0.05) {
        time = Number(next.toFixed(2));
        mod.tickOneSplitHighway(highway, state);
        frames++;
    }
    judgment = 'hit';
    time = 3.2;
    mod.tickOneSplitHighway(highway, state);
    assert.equal(state.pendingJudgments.size, 0);
    assert.equal(state.phraseTotal, 1);
    assert.equal(state.phraseHits, 1);
    assert.ok(providerPolls < frames, 'pending provider calls are throttled below rAF frequency');
});

test('a sustain resolving active to hit is counted exactly once', () => {
    const mod = freshPlugin();
    let time = 0.8;
    let judgment = 'active';
    const highway = {
        hasPhraseData: () => true,
        getPhrases: () => [{ start_time: 0, end_time: 10, max_difficulty: 2 }],
        getTime: () => time,
        getNoteStateProvider: () => () => judgment,
        getFilteredNotes: () => [{ t: 0.1, s: 2, f: 4 }],
        getFilteredChords: () => [],
        getMastery: () => 0.5,
    };
    const state = mod.newSplitScoreState();

    mod.tickOneSplitHighway(highway, state);
    judgment = 'hit';
    time = 1;
    mod.tickOneSplitHighway(highway, state);
    time = 1.2;
    mod.tickOneSplitHighway(highway, state);

    assert.equal(state.phraseTotal, 1);
    assert.equal(state.phraseHits, 1);
    assert.equal(state.phraseJudgments.length, 1);
});

test('phrase finalization discards judgments still unresolved after the final poll', () => {
    const mod = freshPlugin();
    let time = 0.95;
    let judgment = 'active';
    const highway = {
        hasPhraseData: () => true,
        getPhrases: () => [
            { start_time: 0, end_time: 1, max_difficulty: 2 },
            { start_time: 1, end_time: 2, max_difficulty: 2 },
        ],
        getTime: () => time,
        getNoteStateProvider: () => () => judgment,
        getFilteredNotes: () => [{ t: 0.9, s: 3, f: 5 }],
        getFilteredChords: () => [],
        getMastery: () => 0.5,
    };
    const state = mod.newSplitScoreState();

    mod.tickOneSplitHighway(highway, state);
    time = 1.05;
    mod.tickOneSplitHighway(highway, state);
    assert.equal(state.pendingJudgments.size, 0);
    assert.equal(state.phrasesScored, 0, 'an unresolved-only phrase has no scored result');

    judgment = 'hit';
    time = 1.8;
    mod.tickOneSplitHighway(highway, state);
    assert.equal(state.phraseTotal, 0, 'discarded prior-phrase work cannot leak forward');
});

test('a correctly-held sustain crossing the phrase boundary is dropped once the final poll reads active', () => {
    const mod = freshPlugin();
    let time = 0.95;
    // Mirrors note_detect's lifecycle: the provider renders 'active' for the
    // sustain's entire ring — even after a 'hit' verdict is committed while the
    // note is still ringing — and only exposes 'hit' after the hold ends.
    let judgment = 'active';
    const highway = {
        hasPhraseData: () => true,
        getPhrases: () => [
            { start_time: 0, end_time: 1, max_difficulty: 2 },
            { start_time: 1, end_time: 2, max_difficulty: 2 },
        ],
        getTime: () => time,
        getNoteStateProvider: () => () => judgment,
        getFilteredNotes: () => [{ t: 0.9, s: 4, f: 6 }],
        getFilteredChords: () => [],
        getMastery: () => 0.5,
    };
    const state = mod.newSplitScoreState();

    mod.tickOneSplitHighway(highway, state);
    // The sustain rings into phrase 2, so the boundary's final poll reads
    // 'active' and the note is hard-cleared with the rest of the pending set.
    time = 1.05;
    mod.tickOneSplitHighway(highway, state);
    assert.equal(state.pendingJudgments.size, 0);
    assert.equal(state.phrasesScored, 0, 'a phrase whose only note was discarded has no scored result');

    // The verdict lands after the hold ends, well past the boundary read.
    judgment = 'hit';
    time = 1.4;
    mod.tickOneSplitHighway(highway, state);
    assert.equal(state.phraseTotal, 0, 'the late-boundary sustain verdict is never counted');
    assert.equal(state.phraseHits, 0);
    assert.equal(state.phraseJudgments.length, 0);
    assert.equal(state.pendingJudgments.size, 0, 'nor does it re-enter the next phrase as pending');
});

test('phrase finalization forces a pending poll before its throttle expires', () => {
    const mod = freshPlugin();
    let time = 0.75;
    let judgment = 'active';
    let providerPolls = 0;
    const highway = {
        hasPhraseData: () => true,
        getPhrases: () => [
            { start_time: 0, end_time: 0.8, max_difficulty: 2 },
            { start_time: 0.8, end_time: 2, max_difficulty: 2 },
        ],
        getTime: () => time,
        getNoteStateProvider: () => () => { providerPolls++; return judgment; },
        getFilteredNotes: () => [{ t: 0.1, s: 1, f: 2 }],
        getFilteredChords: () => [],
        getMastery: () => 0.5,
    };
    const state = mod.newSplitScoreState();

    mod.tickOneSplitHighway(highway, state);
    judgment = 'hit';
    time = 0.81;
    mod.tickOneSplitHighway(highway, state);

    assert.equal(providerPolls, 2);
    assert.equal(state.phrasesScored, 1);
});

test('a forward seek abandons prior work and skips crossed events across or within a phrase', () => {
    const mod = freshPlugin();
    const ctx = playerContext({ session_id: 'split-seek-forward', player_id: 'forward-player' });
    let time = 0.2;
    const highway = {
        hasPhraseData: () => true,
        getPhrases: () => [
            { start_time: 0, end_time: 1, max_difficulty: 2 },
            { start_time: 4, end_time: 5, max_difficulty: 2 },
        ],
        getTime: () => time,
        getNoteStateProvider: () => () => 'hit',
        getFilteredNotes: () => [
            { t: 0.9, s: 1, f: 2 },
            { t: 4.1, s: 2, f: 3 },
        ],
        getFilteredChords: () => [],
        getMastery: () => 0.5,
    };
    const state = mod.newSplitScoreState(ctx);

    mod.tickOneSplitHighway(highway, state);
    time = 4.2;
    mod.tickOneSplitHighway(highway, state);

    assert.equal(state.phrasesScored, 0);
    assert.deepEqual(mod.loadPhraseAttempts(ctx), []);

    let samePhraseTime = 0.2;
    const samePhraseHighway = {
        hasPhraseData: () => true,
        getPhrases: () => [{ start_time: 0, end_time: 10, max_difficulty: 2 }],
        getTime: () => samePhraseTime,
        getNoteStateProvider: () => () => 'hit',
        getFilteredNotes: () => [
            { t: 0.9, s: 1, f: 2 }, // crossed by the seek
            { t: 3.2, s: 2, f: 3 }, // played after the destination
        ],
        getFilteredChords: () => [],
        getMastery: () => 0.5,
    };
    const samePhraseState = mod.newSplitScoreState();
    mod.tickOneSplitHighway(samePhraseHighway, samePhraseState);
    samePhraseTime = 3;
    mod.tickOneSplitHighway(samePhraseHighway, samePhraseState);
    assert.equal(samePhraseState.phraseTotal, 0, 'crossed note is not scored after the seek');

    samePhraseTime = 3.9;
    mod.tickOneSplitHighway(samePhraseHighway, samePhraseState);
    assert.equal(samePhraseState.phraseTotal, 1);
    assert.equal(samePhraseState.phraseJudgments[0].time, 3.2);
});

test('forward discontinuity detection distinguishes a stalled frame from a seek', () => {
    const mod = freshPlugin();

    assert.equal(mod._isForwardScoringDiscontinuity(2, 4.2, 10, 12.2), false,
        'equal playback and wall-time advances are an ordinary stalled frame');
    assert.equal(mod._isForwardScoringDiscontinuity(2, 4.2, 10, 10.1), true,
        'playback advancing far beyond wall time is a forward seek');
    assert.equal(mod._isForwardScoringDiscontinuity(-1, 4.2, -1, 10.1), false,
        'the first observation cannot establish a discontinuity');
});

test('the same judgment key in notes and chords is enqueued and counted once', () => {
    const mod = freshPlugin();
    let providerPolls = 0;
    const duplicate = { t: 0.1, s: 1, f: 2 };
    const highway = {
        hasPhraseData: () => true,
        getPhrases: () => [{ start_time: 0, end_time: 2, max_difficulty: 2 }],
        getTime: () => 0.8,
        getNoteStateProvider: () => () => { providerPolls++; return 'hit'; },
        getFilteredNotes: () => [duplicate],
        getFilteredChords: () => [{ t: 0.1, notes: [{ s: 1, f: 2 }] }],
        getMastery: () => 0.5,
    };
    const state = mod.newSplitScoreState();

    mod.tickOneSplitHighway(highway, state);

    assert.equal(providerPolls, 1);
    assert.equal(state.phraseTotal, 1);
    assert.equal(state.phraseHits, 1);
});

test('a rewind clears pending judgments before replay', () => {
    const mod = freshPlugin();
    let time = 1.8;
    const highway = {
        hasPhraseData: () => true,
        getPhrases: () => [
            { start_time: 0, end_time: 1, max_difficulty: 2 },
            { start_time: 1, end_time: 2, max_difficulty: 2 },
        ],
        getTime: () => time,
        getNoteStateProvider: () => () => 'active',
        getFilteredNotes: () => [{ t: 1.1, s: 1, f: 2 }],
        getFilteredChords: () => [],
        getMastery: () => 0.5,
    };
    const state = mod.newSplitScoreState();

    mod.tickOneSplitHighway(highway, state);
    assert.equal(state.pendingJudgments.size, 1);
    time = 0.05;
    mod.tickOneSplitHighway(highway, state);
    assert.equal(state.pendingJudgments.size, 0);
    assert.equal(state.noteCursor, 0);
    assert.equal(state.phrasesScored, 0, 'rewind must not finalize the abandoned phrase');
});

test('legacy song difficulty and phrase attempts migrate once into overall for a ready profile', () => {
    const legacyDifficultyKey = 'difficulty_ladder.songMastery';
    const legacyAttemptsKey = 'difficulty_ladder.phraseAttempts.v1';
    const stored = {
        [legacyDifficultyKey]: JSON.stringify({
            'song.feedpak::lead': { mastery: 72, instrument: 'guitar' },
        }),
        [legacyAttemptsKey]: JSON.stringify([{
            schema: 'difficulty_ladder.phrase_attempt.v1',
            session_id: 'old-session', song_key: 'song.feedpak::lead',
            instrument: 'guitar', phrase_id: 'old-phrase', hit_rate: 0.9,
        }]),
    };
    const mod = freshPlugin({ stored });
    const ctx = playerContext({ compatibility_adapter: true, role: 'instrumental' });
    const migrated = { ...ctx, instrument: 'guitar', role: 'instrumental', skill: 'overall' };

    assert.equal(mod.migrateLegacyData(ctx), true);
    assert.equal(mod.migrateLegacyData(ctx), true);
    assert.equal(mod.readProgress(migrated).currentDifficulty, 72);
    assert.equal(mod.readProgress(migrated).bestMastery, null);
    assert.equal(mod.loadPhraseAttempts(ctx).length, 1, 'idempotent migration must not duplicate attempts');
    assert.equal(mod.loadPhraseAttempts(ctx)[0].skill, 'overall');
    assert.equal(global.localStorage.getItem(legacyDifficultyKey), stored[legacyDifficultyKey], 'legacy recovery source retained');
    assert.equal(global.localStorage.getItem(legacyAttemptsKey), stored[legacyAttemptsKey], 'legacy recovery source retained');
});

// #82 acceptance criteria: "Add unit tests for ... malformed values". A
// malformed entry must be skipped silently (no throw, no bogus progress
// record) without blocking migration of the other, valid entries in the
// same legacy map.
test('malformed legacy mastery values are skipped without blocking valid entries', () => {
    const legacyDifficultyKey = 'difficulty_ladder.songMastery';
    const stored = {
        [legacyDifficultyKey]: JSON.stringify({
            'good.feedpak::lead': { mastery: 55, instrument: 'guitar' },
            'string-value.feedpak::lead': 'not-a-number',
            'null-value.feedpak::lead': null,
            'no-mastery-field.feedpak::lead': { instrument: 'guitar' },
        }),
    };
    const mod = freshPlugin({ stored });
    const ctx = playerContext({ compatibility_adapter: true, role: 'instrumental' });

    // NaN has no JSON representation (JSON.stringify silently turns it into
    // `null`), so a real NaN can only reach _masteryPct via the in-memory
    // cache, not a localStorage round-trip — saveSongMasteryMap() sets that
    // cache directly, merged over what freshPlugin() already parsed from
    // `stored` above.
    mod.saveSongMasteryMap(Object.assign(mod.loadSongMasteryMap(), {
        'nan-value.feedpak::lead': { mastery: NaN, instrument: 'guitar' },
    }));

    assert.doesNotThrow(() => mod.migrateLegacyData(ctx));

    const good = { ...ctx, song_id: 'good.feedpak', arrangement_id: 'lead', instrument: 'guitar', role: 'instrumental', skill: 'overall' };
    assert.equal(mod.readProgress(good).currentDifficulty, 55, 'the one well-formed entry still migrates');

    for (const songId of ['string-value.feedpak', 'null-value.feedpak', 'nan-value.feedpak', 'no-mastery-field.feedpak']) {
        const malformed = { ...ctx, song_id: songId, arrangement_id: 'lead', instrument: 'guitar', role: 'instrumental', skill: 'overall' };
        assert.equal(mod.readProgress(malformed), null, `malformed entry ${songId} must not produce a progress record`);
    }
});

// #82 acceptance criteria: "Add unit tests for ... Windows/path-normalized
// filenames". A legacy key containing a raw backslash (as a pre-rename
// client might have stored, before filenames were consistently
// percent-encoded) must round-trip through the split/trim in
// _legacySongIdentity unchanged — no separator normalization is applied
// anywhere in this path, so the migrated song_id must match the legacy key
// substring byte-for-byte, not a silently mangled or re-encoded variant.
test('a legacy key containing a Windows-style path round-trips its song_id exactly', () => {
    const legacyDifficultyKey = 'difficulty_ladder.songMastery';
    const windowsKey = 'C:\\Music\\song.feedpak::lead';
    const stored = {
        [legacyDifficultyKey]: JSON.stringify({
            [windowsKey]: { mastery: 63, instrument: 'guitar' },
        }),
    };
    const mod = freshPlugin({ stored });
    const ctx = playerContext({ compatibility_adapter: true, role: 'instrumental' });

    assert.equal(mod.migrateLegacyData(ctx), true);

    const migrated = {
        ...ctx, song_id: 'C:\\Music\\song.feedpak', arrangement_id: 'lead',
        instrument: 'guitar', role: 'instrumental', skill: 'overall',
    };
    assert.equal(mod.readProgress(migrated).currentDifficulty, 63, 'the backslash-bearing song_id must be preserved exactly, not split/escaped differently');
});

test('explicit concurrent contexts cannot claim unscoped legacy data', () => {
    const mod = freshPlugin({ stored: {
        'difficulty_ladder.songMastery': JSON.stringify({ 'song.feedpak::lead': 99 }),
    } });
    const ctx = playerContext({ compatibility_adapter: false });
    assert.equal(mod.migrateLegacyData(ctx), false);
    assert.equal(mod.readProgress(ctx), null);
});

test('_dominantSongMastery does not show a false 0% badge for an arrangement with only bestMastery set', () => {
    const mod = freshPlugin();
    const ctx = playerContext({ player_id: 'main', song_id: 'song.feedpak', arrangement_id: '0' });
    mod.upsertPlayerContext(ctx);
    // Only bestMastery is set — currentDifficulty stays at its unset null.
    mod.writeProgress(ctx, { bestMastery: 70 });

    assert.equal(mod._dominantSongMastery({ filename: 'song.feedpak' }), null);
});

test('legacy fretted role-less difficulty matches an active lead guitar context', () => {
    const mod = freshPlugin({ stored: {
        'difficulty_ladder.songMastery': JSON.stringify({
            'song.feedpak::lead': { mastery: 68, instrument: 'fretted' },
        }),
    } });
    const claimant = playerContext({ compatibility_adapter: true, player_id: 'main' });
    mod.migrateLegacyData(claimant);

    const lead = playerContext({ player_id: 'main', instrument: 'guitar', role: 'lead', skill: 'overall' });
    const migrated = mod.readProgress(lead);
    assert.equal(migrated.currentDifficulty, 68);
    assert.equal(migrated.legacyUnscoped, true);
    assert.equal(migrated.legacy_claim_player_id, 'main');
});

test('a role-less legacy record with a known instrument does not leak into a different instrument', () => {
    const mod = freshPlugin({ stored: {
        'difficulty_ladder.songMastery': JSON.stringify({
            'song.feedpak::lead': { mastery: 68, instrument: 'fretted' },
        }),
    } });
    const claimant = playerContext({ compatibility_adapter: true, player_id: 'main' });
    mod.migrateLegacyData(claimant);

    // Same profile/song/arrangement/player as the migrated guitar record,
    // but a different instrument (keys) — must not inherit the guitar
    // player's migrated progress just because both are legacyUnscoped.
    const keys = playerContext({ player_id: 'main', instrument: 'keys', role: 'instrumental', skill: 'overall' });
    assert.equal(mod.readProgress(keys), null);

    const lead = playerContext({ player_id: 'main', instrument: 'guitar', role: 'lead', skill: 'overall' });
    assert.equal(mod.readProgress(lead).currentDifficulty, 68);
});

test('malformed v2 and legacy stores fail closed to valid empty shapes', () => {
    const mod = freshPlugin({ stored: {
        'difficulty_ladder.progress.v2': '[]',
        'difficulty_ladder.phraseAttempts.v2': '{broken',
        'difficulty_ladder.songMastery': 'not-json',
        'difficulty_ladder.phraseAttempts.v1': '{"not":"an-array"}',
    } });
    const ctx = playerContext({ compatibility_adapter: true });
    assert.equal(mod.loadProgressStore().schema, 'difficulty_ladder.progress.v2');
    assert.equal(mod.loadPhraseAttemptStore().schema, 'difficulty_ladder.phrase_attempts.v2');
    assert.doesNotThrow(() => mod.migrateLegacyData(ctx));
    assert.equal(mod.readProgress(ctx), null);
    assert.deepEqual(mod.loadPhraseAttempts(ctx), []);
});

test('pending profile readiness gates all v2 writes until identity resolves', async () => {
    const writes = [];
    let resolveProfile;
    const profilePromise = new Promise(resolve => { resolveProfile = resolve; });
    const mod = freshPlugin({ onSet: key => writes.push(key) });
    global.window.v3Profile = { get: () => profilePromise };
    global.window.highway = { hasPhraseData: () => false };

    const activation = mod.activateCompatibilityPlayerContext({
        filename: 'song.feedpak', arrangement_index: 0, type: 'lead',
    });
    assert.equal(mod.writeProgress(null, { currentDifficulty: 50 }), false);
    assert.equal(mod.savePhraseAttempts([{ phrase_id: 'must-not-leak' }]), false);
    assert.deepEqual(writes, []);

    resolveProfile({ id: 'alice', player_hash: 'alice-hash' });
    const ctx = await activation;
    assert.equal(ctx.profile_hash, 'alice-hash');
    assert.ok(writes.includes('difficulty_ladder.progress.v2'), 'migration starts only after readiness');
    assert.ok(writes.includes('difficulty_ladder.phraseAttempts.v2'));
});

test('older Host compatibility resolves only to the single legacy-default profile', () => {
    const mod = freshPlugin();
    const ctx = mod.resolveCompatibilityPlayerContext({ filename: 'song.feedpak', arrangement_index: 2 });
    assert.equal(ctx.profile_id, 'legacy-default');
    assert.equal(ctx.player_id, 'main');
    assert.equal(ctx.compatibility_adapter, true);
    assert.equal(mod.writeProgress(ctx, { currentDifficulty: 44 }), true);
    assert.equal(mod.readProgress(ctx).currentDifficulty, 44);
});

test('compatibility context classifies instrument from arrangement_type, not just instrument/type', () => {
    const mod = freshPlugin();
    // The Host's getSongInfo() shape carries the classifier as
    // arrangement_type (the same field _instrumentKind() reads in
    // onSongEvent) — si.type is the WebSocket message discriminator, not
    // the instrument kind, and currentSong may not duplicate it.
    const ctx = mod.resolveCompatibilityPlayerContext({
        filename: 'song.feedpak', arrangement_index: 0, arrangement_type: 'lead',
    });
    assert.equal(ctx.instrument, 'guitar');
    assert.equal(ctx.role, 'lead');
});

test('profile API failures are surfaced, keep writes gated, and can recover later', () => {
    const mod = freshPlugin();
    const events = [];
    global.window.feedBack = {
        playerContexts: { getActive: () => { throw new Error('profile unavailable'); } },
        emit: (name, detail) => events.push({ name, detail }),
    };

    assert.throws(
        () => mod.resolveCompatibilityPlayerContext({ filename: 'song.feedpak' }),
        /profile unavailable/
    );
    assert.equal(mod.activateCompatibilityPlayerContext({ filename: 'song.feedpak' }), null);
    assert.equal(mod.writeProgress(null, { currentDifficulty: 99 }), false);
    assert.equal(events[0].name, 'difficulty:profile-context-error');
    assert.equal(events[0].detail.player_id, 'main');

    global.window.feedBack.playerContexts.getActive = () => ({
        id: 'recovered-profile', player_hash: 'recovered-hash',
    });
    const recovered = mod.activateCompatibilityPlayerContext({
        filename: 'song.feedpak', arrangement_index: 0, type: 'lead',
    });
    assert.equal(recovered.profile_hash, 'recovered-hash');
    assert.equal(mod.writeProgress(recovered, { currentDifficulty: 57 }), true);
    assert.equal(mod.readProgress(recovered).currentDifficulty, 57);
});

test('phrase-attempt caches are isolated by profile', () => {
    const mod = freshPlugin();
    const a = playerContext({ profile_hash: 'a' });
    const b = playerContext({ profile_hash: 'b' });
    mod.savePhraseAttempts([{ phrase_id: 'a-only' }], a);
    mod.savePhraseAttempts([{ phrase_id: 'b-only' }], b);
    assert.deepEqual(mod.loadPhraseAttempts(a).map(x => x.phrase_id), ['a-only']);
    assert.deepEqual(mod.loadPhraseAttempts(b).map(x => x.phrase_id), ['b-only']);
});

test('numeric legacy difficulty safely restores into a normal instrument context', () => {
    const mod = freshPlugin({ stored: {
        'difficulty_ladder.songMastery': JSON.stringify({ 'song.feedpak::lead': 64 }),
    } });
    const migrationContext = playerContext({ compatibility_adapter: true });
    mod.migrateLegacyData(migrationContext);

    const lead = playerContext({ instrument: 'guitar', role: 'lead', skill: 'overall' });
    const otherPlayer = playerContext({ player_id: 'player-2', instrument: 'guitar', role: 'lead' });
    assert.equal(mod.readProgress(lead).currentDifficulty, 64);
    assert.equal(mod.readProgress(lead).bestMastery, null);
    assert.equal(mod.readProgress(lead).legacy_claim_player_id, 'player-1');
    assert.equal(mod.readProgress(otherPlayer), null, 'only the player that claimed unscoped legacy data may read it');
});

test('finalized phrase results update monotonic best mastery without changing current difficulty', () => {
    const mod = freshPlugin();
    const ctx = playerContext({ player_id: 'player-mastery' });
    let liveMastery = 0.8;
    const highway = { getMastery: () => liveMastery };
    const state = mod.newSplitScoreState(ctx);
    mod.writeProgress(ctx, { currentDifficulty: 61 });

    mod.commitSplitPhraseResult(state, highway, 0.5);
    assert.equal(mod.readProgress(ctx).bestMastery, 40, '80% difficulty x 50% hit rate');
    assert.equal(mod.readProgress(ctx).currentDifficulty, 61);

    liveMastery = 0.5;
    mod.commitSplitPhraseResult(state, highway, 0.5);
    assert.equal(mod.readProgress(ctx).bestMastery, 40, 'a lower result cannot reduce the best ever');

    liveMastery = 0.9;
    mod.commitSplitPhraseResult(state, highway, 1);
    assert.equal(mod.readProgress(ctx).bestMastery, 90);
    assert.equal(mod.readProgress(ctx).currentDifficulty, 61);
});

// #83 acceptance criteria: "Handle aborted, partial, looped, seeked, or
// duplicate session events safely" and "0/100 accuracy". The 0%/100% floor
// and ceiling of the ratio input, and an exact-duplicate finalization, are
// this function's own boundary/idempotency responsibilities — whether a
// call is actually a legitimate finalization (vs. one crossed by a seek or
// loop) is decided upstream by the caller, covered separately by #95.
test('best mastery records exactly 0 at 0% accuracy, not null or a skipped write', () => {
    const mod = freshPlugin();
    const ctx = playerContext({ player_id: 'player-zero' });
    const state = mod.newSplitScoreState(ctx);
    mod.writeProgress(ctx, { currentDifficulty: 80 });
    const highway = { getMastery: () => 0.8 };

    mod.commitSplitPhraseResult(state, highway, 0);
    assert.equal(mod.readProgress(ctx).bestMastery, 0, '0% accuracy must be recorded as 0, not treated as unset');
});

test('best mastery at 100% accuracy equals the full presented difficulty', () => {
    const mod = freshPlugin();
    const ctx = playerContext({ player_id: 'player-full' });
    const state = mod.newSplitScoreState(ctx);
    mod.writeProgress(ctx, { currentDifficulty: 80 });
    const highway = { getMastery: () => 0.8 };

    mod.commitSplitPhraseResult(state, highway, 1);
    assert.equal(mod.readProgress(ctx).bestMastery, 80, '100% hit rate at 80% difficulty is worth the full 80');
});

// #83 acceptance criteria: "difficulty bounds". _phraseMasteryPct clamps
// both inputs to 0..1 before multiplying, independent of the 0/100 ratio
// boundary tests above (those exercise the ratio edge; this exercises the
// difficulty edge, and an out-of-range ratio at the same time).
test('best mastery clamps an out-of-range ratio or difficulty instead of over/under-shooting', () => {
    const mod = freshPlugin();
    const overRatioCtx = playerContext({ player_id: 'player-over-ratio' });
    mod.writeProgress(overRatioCtx, { currentDifficulty: 80 });
    mod.commitSplitPhraseResult(mod.newSplitScoreState(overRatioCtx), { getMastery: () => 0.8 }, 1.5);
    assert.equal(mod.readProgress(overRatioCtx).bestMastery, 80, 'a ratio above 1 clamps to 1, not an inflated >80 mastery');

    const underDifficultyCtx = playerContext({ player_id: 'player-under-difficulty' });
    mod.writeProgress(underDifficultyCtx, { currentDifficulty: 50 });
    mod.commitSplitPhraseResult(mod.newSplitScoreState(underDifficultyCtx), { getMastery: () => -0.2 }, 1);
    assert.equal(mod.readProgress(underDifficultyCtx).bestMastery, 0, 'a negative difficulty clamps to 0, not a negative mastery');
});

// #83 acceptance criteria: "missing accuracy". A non-finite ratio (no
// judgment data to compute a hit rate from) must leave bestMastery
// untouched, not write NaN or a bogus 0.
//
// The fresh-context case below is the load-bearing assertion: on a node
// with no prior bestMastery, writeProgress's monotonic check short-circuits
// on `previousBest === null` regardless of the incoming value, so a NaN
// ratio is only actually caught by _phraseMasteryPct's/_pct's own isFinite
// guards, not by the `mastery > previousBest` comparison. (An earlier
// version of this test wrote a real value first and only checked NaN
// afterward — `NaN > 30` is always false, so that ordering silently passed
// even with both isFinite guards removed. Confirmed by mutation testing.)
test('a non-finite (missing) accuracy ratio leaves best mastery unchanged', () => {
    const mod = freshPlugin();
    const freshCtx = playerContext({ player_id: 'player-missing-accuracy-fresh' });
    mod.writeProgress(freshCtx, { currentDifficulty: 60 });
    mod.commitSplitPhraseResult(mod.newSplitScoreState(freshCtx), { getMastery: () => 0.6 }, NaN);
    assert.equal(mod.readProgress(freshCtx).bestMastery, null, 'a NaN ratio on a fresh node must not write anything, not even 0 or NaN itself');

    const establishedCtx = playerContext({ player_id: 'player-missing-accuracy-established' });
    const state = mod.newSplitScoreState(establishedCtx);
    mod.writeProgress(establishedCtx, { currentDifficulty: 60 });
    const highway = { getMastery: () => 0.6 };
    mod.commitSplitPhraseResult(state, highway, 0.5);
    assert.equal(mod.readProgress(establishedCtx).bestMastery, 30, 'sanity: a real ratio does record');
    mod.commitSplitPhraseResult(state, highway, NaN);
    assert.equal(mod.readProgress(establishedCtx).bestMastery, 30, 'a missing/non-finite ratio must not overwrite an existing best mastery either');
});

// #82 acceptance criteria: "duplicate arrangements". Two different
// arrangements of the same song, both present in the legacy map, must
// migrate independently — same song_id, different arrangement_id in the
// compound progress key, no collision or cross-write.
test('two arrangements of the same song migrate independently without colliding', () => {
    const legacyDifficultyKey = 'difficulty_ladder.songMastery';
    const stored = {
        [legacyDifficultyKey]: JSON.stringify({
            'shared.feedpak::lead': { mastery: 40, instrument: 'guitar' },
            'shared.feedpak::rhythm': { mastery: 65, instrument: 'guitar' },
        }),
    };
    const mod = freshPlugin({ stored });
    const ctx = playerContext({ compatibility_adapter: true, role: 'instrumental' });

    assert.equal(mod.migrateLegacyData(ctx), true);

    const lead = { ...ctx, song_id: 'shared.feedpak', arrangement_id: 'lead', instrument: 'guitar', role: 'instrumental', skill: 'overall' };
    const rhythm = { ...ctx, song_id: 'shared.feedpak', arrangement_id: 'rhythm', instrument: 'guitar', role: 'instrumental', skill: 'overall' };
    assert.equal(mod.readProgress(lead).currentDifficulty, 40, 'the lead arrangement keeps its own value');
    assert.equal(mod.readProgress(rhythm).currentDifficulty, 65, 'the rhythm arrangement keeps its own, independent value');
});

// #83 acceptance criteria: "arrangement switches". The live scoring path
// (not just migration) must key bestMastery by arrangement_id too — a
// phrase finalized while playing one arrangement must not affect another
// arrangement of the same song.
test('a live phrase finalization does not affect a different arrangement of the same song', () => {
    const mod = freshPlugin();
    const lead = playerContext({ player_id: 'player-switch', song_id: 'switch.feedpak', arrangement_id: 'lead' });
    const rhythm = { ...lead, arrangement_id: 'rhythm' };
    mod.writeProgress(lead, { currentDifficulty: 70 });
    mod.writeProgress(rhythm, { currentDifficulty: 30 });

    mod.commitSplitPhraseResult(mod.newSplitScoreState(lead), { getMastery: () => 0.7 }, 1);
    assert.equal(mod.readProgress(lead).bestMastery, 70);
    assert.equal(mod.readProgress(rhythm).bestMastery, null, 'switching arrangements must not leak a best-mastery write across arrangement_id');
});

// Scoped to the persisted best-mastery write only, with auto-adjust off
// (the plugin's own default — see lsGet('autoAdjust', false) — pinned
// explicitly here rather than left implicit). With auto-adjust ON, a
// second identical call is NOT fully idempotent: commitSplitPhraseResult
// still increments state.phrasesScored, can cross WARMUP_PHRASES, and can
// advance the ramp / call setMastery a second time for what should be one
// phrase's worth of evidence. That's a property of whether the caller
// de-duplicates the underlying event before invoking this function at all
// (PR #95's territory, same as the seek/loop carve-out above), not of the
// best-mastery formula this test targets — so it isn't asserted here.
test('an exact-duplicate finalization does not change the persisted best-mastery record', () => {
    const mod = freshPlugin({ stored: { 'difficulty_ladder.autoAdjust': 'false' } });
    const ctx = playerContext({ player_id: 'player-dup' });
    const state = mod.newSplitScoreState(ctx);
    mod.writeProgress(ctx, { currentDifficulty: 70 });
    const highway = { getMastery: () => 0.7 };

    mod.commitSplitPhraseResult(state, highway, 0.5);
    assert.equal(mod.readProgress(ctx).bestMastery, 35);
    const firstUpdatedAt = mod.readProgress(ctx).updatedAt;

    // Same context, same highway, same ratio — as if the same phrase
    // finalization event fired twice (a plausible duplicate-event shape,
    // independent of whatever upstream guard is meant to prevent it).
    mod.commitSplitPhraseResult(state, highway, 0.5);
    assert.equal(mod.readProgress(ctx).bestMastery, 35, 'a repeat of the same result must not change the best-ever value');
    assert.equal(mod.readProgress(ctx).updatedAt, firstUpdatedAt, 'a no-op write must not touch updatedAt either');
});

test('main-player phrase finalization updates best mastery through the compatibility context', () => {
    const mod = freshPlugin();
    global.window.highway = { getMastery: () => 0.7 };
    const ctx = mod.activateCompatibilityPlayerContext({
        filename: 'song.feedpak', arrangement_index: 0, type: 'lead',
    });

    mod.commitPhraseResult(0.8);

    assert.equal(mod.readProgress(ctx).bestMastery, 56);
    assert.equal(mod.readProgress(ctx).currentDifficulty, null);
});

test('a compatibility profile switch on the same song resets main scoring state', () => {
    const mod = freshPlugin();
    global.window.highway = { getMastery: () => 0.8, hasPhraseData: () => false };
    let activeProfile = { id: 'alice', player_hash: 'alice-hash' };
    global.window.v3Profile = { get: () => activeProfile };
    const si = { filename: 'song.feedpak', arrangement_index: 0, type: 'lead' };

    mod.activateCompatibilityPlayerContext(si);
    mod.settings.autoAdjust = false;
    mod.settings.maxMastery = 80;
    for (let i = 1; i <= mod.MASTERY_STREAK_PHRASES; i++) mod.commitPhraseResult(mod.MASTERY_STREAK_ACCURACY);
    assert.deepEqual(mod.masteryStreakStatus(), { count: mod.MASTERY_STREAK_PHRASES, active: true },
        'streak built up under alice');

    // song:ready can re-fire for the same song (reconnect/restart) without
    // _songKey changing. A different active profile must not let the new
    // player inherit alice's in-progress streak/EMA/warm-up state.
    activeProfile = { id: 'bob', player_hash: 'bob-hash' };
    mod.activateCompatibilityPlayerContext(si);
    assert.deepEqual(mod.masteryStreakStatus(), { count: 0, active: false },
        'bob must not inherit alice\'s streak just because the song stayed the same');
});

test('recordPhraseAttempt fails closed when a malformed scoped node cannot be created', () => {
    const ctx = playerContext();
    // Node keys are 'k_' + encodeURIComponent(value) (see _nodeKey in
    // screen.js) — 'hash-1' has no characters encodeURIComponent touches.
    const malformed = {
        schema: 'difficulty_ladder.phrase_attempts.v2', version: 2,
        profiles: { 'k_hash-1': 'not-an-object' }, migrations: {},
    };
    const mod = freshPlugin({ stored: {
        'difficulty_ladder.phraseAttempts.v2': JSON.stringify(malformed),
    } });
    const state = mod.newSplitScoreState(ctx);
    state.curPhraseIdx = 0;
    state.phraseTotal = 1;
    state.phraseHits = 1;
    const highway = {
        getPhrases: () => [{ start_time: 0, end_time: 1, max_difficulty: 1 }],
        getMastery: () => 0.5,
    };

    assert.doesNotThrow(() => mod.recordPhraseAttempt(1, ctx, state, highway));
    assert.equal(mod.recordPhraseAttempt(1, ctx, state, highway), false);
});

test('recordPhraseAttempt returns false when the current phrase has no stable id', () => {
    const mod = freshPlugin();
    const ctx = playerContext();
    const state = mod.newSplitScoreState(ctx);
    state.curPhraseIdx = 3;
    state.phraseTotal = 1;
    state.phraseHits = 1;
    const highway = { getPhrases: () => [], getMastery: () => 0.5 };

    assert.equal(mod.recordPhraseAttempt(1, ctx, state, highway), false);
    assert.deepEqual(mod.loadPhraseAttempts(ctx), []);
});

test('player context change resets split scorer state and restores only the new identity', () => {
    const mod = freshPlugin();
    const oldContext = playerContext({ profile_hash: 'old-profile' });
    const newContext = playerContext({ profile_hash: 'new-profile', instrument: 'bass', role: 'bass' });
    let mastery = 0.5;
    const highway = {
        hasPhraseData: () => true,
        getMastery: () => mastery,
        setMastery: value => { mastery = value; },
    };
    mod.writeProgress(newContext, { currentDifficulty: 31 });
    mod.registerSplitHighway(highway, oldContext);
    mod.upsertPlayerContext(oldContext);
    const state = mod._splitScoreStateForHighway(highway);
    state.curPhraseIdx = 4;
    state.phraseHits = 8;
    state.phraseTotal = 10;
    state.phrasesScored = 9;
    state.emaHitRate = 0.8;
    state.manualOverride = true;
    state.pendingJudgments.set('stale', { key: 'stale' });

    mod.upsertPlayerContext(newContext);

    assert.equal(state.context.profile_hash, 'new-profile');
    assert.equal(state.context.instrument, 'bass');
    assert.equal(state.curPhraseIdx, -1);
    assert.equal(state.phraseTotal, 0);
    assert.equal(state.phrasesScored, 0);
    assert.equal(state.emaHitRate, null);
    assert.equal(state.manualOverride, false);
    assert.equal(state.pendingJudgments.size, 0);
    assert.equal(mastery, 0.31);
});

test('manual override disables only the affected split-player controller', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    const stateA = mod.newSplitScoreState(playerContext({ player_id: 'player-a' }));
    const stateB = mod.newSplitScoreState(playerContext({ player_id: 'player-b', profile_hash: 'hash-b' }));
    let masteryA = 0.6;
    let masteryB = 0.5;
    const highwayA = { getMastery: () => masteryA, setMastery: value => { masteryA = value; } };
    const highwayB = { getMastery: () => masteryB, setMastery: value => { masteryB = value; } };
    stateA.phrasesScored = 1; // controller has completed warm-up and has an observed value
    stateA.lastObservedMasteryPct = 50; // live 60% means this pane was moved manually

    mod.commitSplitPhraseResult(stateA, highwayA, 1);
    mod.commitSplitPhraseResult(stateB, highwayB, 1);
    mod.commitSplitPhraseResult(stateB, highwayB, 1);

    assert.equal(stateA.manualOverride, true);
    assert.equal(mod.settings.autoAdjust, true, 'other players retain the global enablement');
    assert.equal(masteryA, 0.6);
    assert.equal(masteryB, 0.55);
});

test('two untagged split panels receive distinct stable controller identities', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    let masteryA = 0.6;
    let masteryB = 0.5;
    const highwayA = {
        hasPhraseData: () => false,
        getMastery: () => masteryA,
        setMastery: value => { masteryA = value; },
    };
    const highwayB = {
        hasPhraseData: () => false,
        getMastery: () => masteryB,
        setMastery: value => { masteryB = value; },
    };
    mod.registerSplitHighway(highwayA);
    mod.registerSplitHighway(highwayB);
    const stateA = mod._splitScoreStateForHighway(highwayA);
    const stateB = mod._splitScoreStateForHighway(highwayB);

    assert.ok(stateA.playerKey);
    assert.ok(stateB.playerKey);
    assert.notEqual(stateA.playerKey, stateB.playerKey);
    stateA.phrasesScored = 1;
    stateA.lastObservedMasteryPct = 50;
    mod.commitSplitPhraseResult(stateA, highwayA, 1);
    mod.commitSplitPhraseResult(stateB, highwayB, 1);
    mod.commitSplitPhraseResult(stateB, highwayB, 1);

    assert.equal(stateA.manualOverride, true);
    assert.equal(stateB.manualOverride, false);
    assert.equal(masteryA, 0.6);
    assert.equal(masteryB, 0.55, 'one untagged pane override must not disable its sibling');
    assert.equal(mod.readProgress(playerContext()), null, 'untagged panes remain persistence-gated');
});

test('phrase attempts are isolated and capped by the complete persistence path', () => {
    const mod = freshPlugin();
    const overallGuitar = playerContext({ instrument: 'guitar', role: 'lead', skill: 'overall' });
    const pinchGuitar = playerContext({ instrument: 'guitar', role: 'lead', skill: 'pinch-harmonics' });
    const bass = playerContext({ instrument: 'bass', role: 'bass', skill: 'overall' });
    const many = Array.from({ length: 5001 }, (_, i) => ({ phrase_id: `g-${i}` }));

    mod.savePhraseAttempts(many, overallGuitar);
    mod.savePhraseAttempts([{ phrase_id: 'pinch-only' }], pinchGuitar);
    mod.savePhraseAttempts([{ phrase_id: 'bass-only' }], bass);

    assert.equal(mod.loadPhraseAttempts(overallGuitar).length, 5000);
    assert.equal(mod.loadPhraseAttempts(overallGuitar)[0].phrase_id, 'g-1');
    assert.deepEqual(mod.loadPhraseAttempts(pinchGuitar).map(x => x.phrase_id), ['pinch-only']);
    assert.deepEqual(mod.loadPhraseAttempts(bass).map(x => x.phrase_id), ['bass-only']);
});

test('upsertPlayerContext never registers the main player as a split scorer', () => {
    const mod = freshPlugin();
    const highway = { hasPhraseData: () => false };
    // A Host is free to include a highway reference on the main player's
    // context payload too — this must not double-register it as a split
    // scorer, since tickScoring()'s own window.highway path already scores
    // the main player every frame.
    const ctx = playerContext({ player_id: 'main', highway: highway });

    mod.upsertPlayerContext(ctx);

    assert.equal(mod._splitScoreStateForHighway(highway), undefined,
        'the main highway must not be scored a second time via the split-scorer map');
});

test('player leave removes linked detector state using only stable context ids', () => {
    const mod = freshPlugin();
    const ctx = playerContext({ session_id: 'session-leave', player_id: 'player-4' });
    const highway = { hasPhraseData: () => false };
    mod.registerSplitHighway(highway, ctx);
    mod.upsertPlayerContext(ctx);
    assert.ok(mod._splitScoreStateForHighway(highway));

    assert.equal(mod.removePlayerContext({ session_id: 'session-leave', player_id: 'player-4' }), true);
    assert.equal(mod._splitScoreStateForHighway(highway), undefined);
});

test('player leave without session_id uses the same implicit session as context upsert', () => {
    const mod = freshPlugin();
    const ctx = playerContext({ session_id: undefined, player_id: 'implicit-session-player' });
    const highway = { hasPhraseData: () => false };
    mod.registerSplitHighway(highway, ctx);
    mod.upsertPlayerContext(ctx);
    assert.ok(mod._splitScoreStateForHighway(highway));

    assert.equal(mod.removePlayerContext({ player_id: 'implicit-session-player' }), true);
    assert.equal(mod._splitScoreStateForHighway(highway), undefined);
});

test('split adaptive applies schedule pane-scoped section refresh events', async () => {
    const mod = freshPlugin();
    const events = [];
    global.window.feedBack = { emit: (name, detail) => events.push({ name, detail }) };
    function scopedHighway(initial) {
        let mastery = initial;
        return {
            hasPhraseData: () => true,
            getSections: () => [{ time: 0 }],
            getPhrases: () => [{ start_time: 0, end_time: 10, max_difficulty: 3 }],
            getMastery: () => mastery,
            setMastery: value => { mastery = value; },
        };
    }
    const a = playerContext({ player_id: 'player-a', profile_hash: 'hash-a' });
    const b = playerContext({ player_id: 'player-b', profile_hash: 'hash-b' });
    const highwayA = scopedHighway(0.5);
    const highwayB = scopedHighway(0.5);

    mod._applyDifficultyForContext(a, 60, highwayA, 'adaptive');
    mod._applyDifficultyForContext(b, 70, highwayB, 'adaptive');
    await new Promise(resolve => setTimeout(resolve, 200));

    const sectionEvents = events.filter(event => event.name === 'difficulty:sections-updated');
    assert.equal(sectionEvents.length, 2);
    const byPlayer = Object.fromEntries(sectionEvents.map(event => [
        event.detail.player_context.player_id, event.detail.mastery,
    ]));
    assert.deepEqual(byPlayer, { 'player-a': 0.6, 'player-b': 0.7 });
});

test('main song lifecycle does not cancel a pending split-pane section refresh', async () => {
    const mod = freshPlugin();
    const events = [];
    global.window.feedBack = {
        currentSong: { filename: 'main.feedpak', arrangementIndex: 0 },
        emit: (name, detail) => events.push({ name, detail }),
    };
    const splitContext = playerContext({ player_id: 'side-player', profile_hash: 'side-profile' });
    let splitMastery = 0.5;
    const splitHighway = {
        hasPhraseData: () => true,
        getSections: () => [{ time: 0 }],
        getPhrases: () => [{ start_time: 0, end_time: 10, max_difficulty: 2 }],
        getMastery: () => splitMastery,
        setMastery: value => { splitMastery = value; },
    };
    mod._applyDifficultyForContext(splitContext, 65, splitHighway, 'adaptive');
    global.window.highway = {
        getSongInfo: () => ({ arrangement_index: 0, type: 'lead' }),
        hasPhraseData: () => false,
    };

    mod.onSongEvent();
    await new Promise(resolve => setTimeout(resolve, 200));

    const splitEvents = events.filter(event => event.name === 'difficulty:sections-updated'
        && event.detail.player_context?.player_id === 'side-player');
    assert.equal(splitEvents.length, 1);
    assert.equal(splitEvents[0].detail.mastery, 0.65);
});

test('only main player context events retarget global mastery persistence', () => {
    const mod = freshPlugin();
    global.window.highway = { hasPhraseData: () => true };
    const mainA = playerContext({ player_id: 'main', profile_hash: 'main-a' });
    const side = playerContext({ player_id: 'player-2', profile_hash: 'side' });
    const mainB = playerContext({ player_id: 'main', profile_hash: 'main-b' });

    mod.upsertPlayerContext(mainA);
    mod._onMasteryApplied(25);
    mod.upsertPlayerContext(side);
    mod._onMasteryApplied(30);
    mod.upsertPlayerContext(mainB);
    mod._onMasteryApplied(80);

    assert.equal(mod.readProgress(mainA).currentDifficulty, 30);
    assert.equal(mod.readProgress(side), null);
    assert.equal(mod.readProgress(mainB).currentDifficulty, 80);
});

test('karaoke canonicalizes explicit vocals instrument aliases to voice', () => {
    const mod = freshPlugin();
    const ctx = mod.normalizePlayerContext(playerContext({ instrument: 'vocals', role: 'karaoke' }));
    assert.equal(ctx.instrument, 'voice');
    assert.equal(ctx.role, 'karaoke');
});

test('changing a detector player key resets state even when persistence identity matches', () => {
    const mod = freshPlugin();
    const first = playerContext({ player_id: 'player-1' });
    const second = playerContext({ player_id: 'player-2' });
    const highway = { hasPhraseData: () => false };
    mod.registerSplitHighway(highway, first);
    const state = mod._splitScoreStateForHighway(highway);
    state.emaHitRate = 0.9;
    state.phrasesScored = 8;
    state.manualOverride = true;

    mod.registerSplitHighway(highway, second);

    assert.equal(state.playerKey, mod.playerContextKey(second));
    assert.equal(state.emaHitRate, null);
    assert.equal(state.phrasesScored, 0);
    assert.equal(state.manualOverride, false);
});

test('replacing a player highway disposes the old scorer state', () => {
    const mod = freshPlugin();
    const ctx = playerContext({ player_id: 'player-3' });
    const oldHighway = { hasPhraseData: () => false };
    const newHighway = { hasPhraseData: () => false };
    mod.registerSplitHighway(oldHighway, ctx);
    mod._splitScoreStateForHighway(oldHighway).emaHitRate = 0.75;

    mod.registerSplitHighway(newHighway, ctx);

    assert.equal(mod._splitScoreStateForHighway(oldHighway), undefined);
    assert.equal(mod._splitScoreStateForHighway(newHighway).emaHitRate, null);
});

test('ready split context emits initial scoped sections without saved difficulty', async () => {
    const mod = freshPlugin();
    const events = [];
    global.window.feedBack = { emit: (name, detail) => events.push({ name, detail }) };
    const ctx = playerContext({ player_id: 'player-initial', profile_hash: 'initial-profile' });
    const highway = {
        hasPhraseData: () => true,
        getSections: () => [{ time: 0 }],
        getPhrases: () => [{ start_time: 0, end_time: 8, max_difficulty: 2 }],
        getMastery: () => 0.45,
    };

    mod.registerSplitHighway(highway, ctx);
    await new Promise(resolve => setTimeout(resolve, 200));

    const event = events.find(item => item.name === 'difficulty:sections-updated');
    assert.ok(event);
    assert.equal(event.detail.player_context.player_id, 'player-initial');
    assert.equal(event.detail.mastery, 0.45);
});

test('async main-profile readiness emits scoped sections without saved difficulty', async () => {
    const mod = freshPlugin();
    const events = [];
    let resolveProfile;
    global.window.feedBack = { emit: (name, detail) => events.push({ name, detail }) };
    global.window.v3Profile = {
        get: () => new Promise(resolve => { resolveProfile = resolve; }),
    };
    global.window.highway = {
        hasPhraseData: () => true,
        getSections: () => [{ time: 0 }],
        getPhrases: () => [{ start_time: 0, end_time: 8, max_difficulty: 2 }],
        getMastery: () => 0.55,
    };

    const activation = mod.activateCompatibilityPlayerContext({
        filename: 'song.feedpak', arrangement_index: 0, type: 'lead',
    });
    resolveProfile({ id: 'main-profile', player_hash: 'main-profile-hash' });
    await activation;
    await new Promise(resolve => setTimeout(resolve, 200));

    const sectionEvents = events.filter(item => item.name === 'difficulty:sections-updated');
    assert.equal(sectionEvents.length, 1);
    assert.equal(sectionEvents[0].detail.player_context.player_id, 'main');
    assert.equal(sectionEvents[0].detail.player_context.profile_hash, 'main-profile-hash');
    assert.equal(sectionEvents[0].detail.mastery, 0.55);
});

test('song ready waits for profile readiness before its first Section Map emission', async () => {
    const mod = freshPlugin();
    const events = [];
    let resolveProfile;
    global.window.feedBack = {
        currentSong: { filename: 'song.feedpak', arrangementIndex: 0 },
        emit: (name, detail) => events.push({ name, detail }),
    };
    global.window.v3Profile = {
        get: () => new Promise(resolve => { resolveProfile = resolve; }),
    };
    global.window.highway = {
        getSongInfo: () => ({ arrangement_index: 0, type: 'lead' }),
        hasPhraseData: () => true,
        getSections: () => [{ time: 0 }],
        getPhrases: () => [{ start_time: 0, end_time: 8, max_difficulty: 2 }],
        getMastery: () => 0.5,
    };

    mod.onSongEvent();
    assert.equal(
        events.filter(item => item.name === 'difficulty:sections-updated').length,
        0,
        'no null-context event may escape while the profile promise is pending'
    );

    resolveProfile({ id: 'ready-profile', player_hash: 'ready-profile-hash' });
    await new Promise(resolve => setTimeout(resolve, 200));

    const sectionEvents = events.filter(item => item.name === 'difficulty:sections-updated');
    assert.equal(sectionEvents.length, 1);
    assert.equal(sectionEvents[0].detail.player_context.player_id, 'main');
    assert.equal(sectionEvents[0].detail.player_context.profile_hash, 'ready-profile-hash');
});

test('null-instrument v1 phrase attempts use only their claimed profile and player fallback', () => {
    const mod = freshPlugin({ stored: {
        'difficulty_ladder.phraseAttempts.v1': JSON.stringify([{
            schema: 'difficulty_ladder.phrase_attempt.v1',
            session_id: 'old-session', song_key: 'song.feedpak::lead',
            phrase_id: 'legacy-no-instrument', hit_rate: 0.8,
        }]),
    } });
    const claimant = playerContext({ compatibility_adapter: true, player_id: 'main' });
    mod.migrateLegacyData(claimant);

    const normal = playerContext({ player_id: 'main', instrument: 'guitar', role: 'lead' });
    const otherPlayer = playerContext({ player_id: 'player-2', instrument: 'guitar', role: 'lead' });
    const otherProfile = playerContext({
        player_id: 'main', profile_id: 'profile-2', profile_hash: 'hash-2',
        instrument: 'guitar', role: 'lead',
    });
    assert.deepEqual(mod.loadPhraseAttempts(normal).map(x => x.phrase_id), ['legacy-no-instrument']);
    assert.equal(mod.loadPhraseAttempts(normal)[0].legacy_unscoped_instrument, true);
    assert.deepEqual(mod.loadPhraseAttempts(otherPlayer), []);
    assert.deepEqual(mod.loadPhraseAttempts(otherProfile), []);
});

test('re-enabling auto difficulty preserves other players manual opt-outs', () => {
    const mod = freshPlugin();
    const main = playerContext({ player_id: 'main', profile_hash: 'main-profile' });
    const sideA = playerContext({ player_id: 'player-a', profile_hash: 'a-profile' });
    const sideB = playerContext({ player_id: 'player-b', profile_hash: 'b-profile' });
    const mainHighway = { hasPhraseData: () => false };
    const highwayA = { hasPhraseData: () => false };
    const highwayB = { hasPhraseData: () => false };
    mod.registerSplitHighway(mainHighway, main);
    mod.registerSplitHighway(highwayA, sideA);
    mod.registerSplitHighway(highwayB, sideB);
    mod.upsertPlayerContext(main);
    const mainState = mod._splitScoreStateForHighway(mainHighway);
    const stateA = mod._splitScoreStateForHighway(highwayA);
    const stateB = mod._splitScoreStateForHighway(highwayB);
    mainState.manualOverride = true;
    stateA.manualOverride = true;
    stateB.manualOverride = true;

    global.window.dispatchEvent({
        type: 'difficulty_ladder:settings-changed',
        detail: { autoAdjust: true },
    });

    assert.equal(mainState.manualOverride, false, 'current main controller follows single-player re-enable UX');
    assert.equal(stateA.manualOverride, true);
    assert.equal(stateB.manualOverride, true);
});

// ── reactionSpeed / EMA_ALPHA (issue #5) ────────────────────────────────────

test('emaAlpha() at the default reactionSpeed (2) matches the plugin\'s original hardcoded EMA_ALPHA (0.35)', () => {
    const mod = freshPlugin();
    mod.settings.reactionSpeed = 2;
    assert.ok(Math.abs(mod.emaAlpha() - 0.35) < 1e-9);
});

test('emaAlpha() clamps reactionSpeed to [1,3] and spans slow(0.20)..fast(0.50)', () => {
    const mod = freshPlugin();
    mod.settings.reactionSpeed = 1;
    assert.ok(Math.abs(mod.emaAlpha() - 0.20) < 1e-9);
    mod.settings.reactionSpeed = 3;
    assert.ok(Math.abs(mod.emaAlpha() - 0.50) < 1e-9);
    mod.settings.reactionSpeed = 0; // out of range -> clamps to 1's value
    assert.ok(Math.abs(mod.emaAlpha() - 0.20) < 1e-9);
});

test('emaAlpha() and thresholds() are independent axes (sensitivity does not move emaAlpha and vice versa)', () => {
    const mod = freshPlugin();
    mod.settings.sensitivity = 3;
    mod.settings.reactionSpeed = 1;
    const th = mod.thresholds();
    assert.equal(th.step, 20); // driven only by sensitivity
    assert.ok(Math.abs(mod.emaAlpha() - 0.20) < 1e-9); // driven only by reactionSpeed
});

// ── Library card badge (issue #4) ───────────────────────────────────────────

test('_dominantSongMastery returns null for a song with no saved mastery', () => {
    const mod = freshPlugin();
    assert.equal(mod._dominantSongMastery({ filename: 'unplayed.feedpak' }), null);
});

test('_dominantSongMastery returns null for a song/undefined with no filename', () => {
    const mod = freshPlugin();
    assert.equal(mod._dominantSongMastery(null), null);
    assert.equal(mod._dominantSongMastery({}), null);
});

test('_dominantSongMastery prefers arrangement 0 when multiple arrangements have saved values', () => {
    const mod = freshPlugin();
    mod.saveSongMasteryMap({ 'song.feedpak::0': 40, 'song.feedpak::1': 90 });
    assert.equal(mod._dominantSongMastery({ filename: 'song.feedpak' }), 40);
});

test('_dominantSongMastery falls back to whichever arrangement has a value when arrangement 0 has none', () => {
    const mod = freshPlugin();
    mod.saveSongMasteryMap({ 'song.feedpak::1': 65 });
    assert.equal(mod._dominantSongMastery({ filename: 'song.feedpak' }), 65);
});

test('_dominantSongMastery does not match a different song sharing a filename prefix', () => {
    const mod = freshPlugin();
    mod.saveSongMasteryMap({ 'song.feedpak::0': 40 });
    assert.equal(mod._dominantSongMastery({ filename: 'song' }), null); // 'song' is not a prefix match of 'song.feedpak::0'
});

test('mastery readers accept instrument-tagged records without breaking legacy numeric records', () => {
    const mod = freshPlugin();
    mod.saveSongMasteryMap({
        'keys.feedpak::0': { mastery: 72, instrument: 'keys' },
        'guitar.feedpak::0': 64,
    });
    assert.equal(mod._dominantSongMastery({ filename: 'keys.feedpak' }), 72);
    assert.equal(mod._dominantSongMastery({ filename: 'guitar.feedpak' }), 64);
});

test('_rememberSongInstrument upgrades an existing mastery record and preserves its identifiers', () => {
    const mod = freshPlugin();
    mod.saveSongMasteryMap({ 'song.feedpak::2': 81 });

    mod._rememberSongInstrument('song.feedpak::2', 'fretted');

    assert.deepEqual(mod.loadSongMasteryMap(), {
        'song.feedpak::2': { mastery: 81, instrument: 'fretted' },
    });
});

test('_rememberSongInstrument persists classification before the first mastery value exists', () => {
    const mod = freshPlugin();

    mod._rememberSongInstrument('new.feedpak::0', 'keys');

    assert.deepEqual(mod.loadSongMasteryMap(), {
        'new.feedpak::0': { mastery: null, instrument: 'keys' },
    });
    assert.equal(mod._dominantSongMastery({ filename: 'new.feedpak' }), null);
});

test('_instrumentKind mirrors the generator classifier for song_info metadata', () => {
    const mod = freshPlugin();
    assert.equal(mod._instrumentKind('bass', 'Bass'), 'fretted');
    assert.equal(mod._instrumentKind('', 'Synth Pad'), 'keys');
    assert.equal(mod._instrumentKind('piano', 'Grand'), 'keys');
    assert.equal(mod._instrumentKind('drums', 'Kit'), 'drums');
    assert.equal(mod._instrumentKind('vocals', 'Lead Vox'), 'unsupported');
});

test('song ready upgrades numeric mastery with authoritative instrument metadata', () => {
    const mod = freshPlugin();
    mod.saveSongMasteryMap({ 'song.feedpak::2': 68 });
    global.window.feedBack = { currentSong: { filename: 'song.feedpak' } };
    global.window.highway = {
        getSongInfo: () => ({ arrangement_index: 2, arrangement_type: 'bass', arrangement: 'Bass' }),
        hasPhraseData: () => false,
    };
    mod.onSongEvent();
    assert.deepEqual(mod.loadSongMasteryMap()['song.feedpak::2'], {
        mastery: 68,
        instrument: 'fretted',
    });
});

test('song-wide generation remembers every supported arrangement classifier', () => {
    const mod = freshPlugin();
    mod.rememberGeneratedInstruments('mixed.feedpak', 1, {
        arrangements: [
            { arrangement_index: 0, instrument: 'fretted' },
            { arrangement_index: 1, instrument: 'keys' },
            { arrangement_index: 2, instrument: 'drums' },
        ],
    });
    assert.deepEqual(mod.loadSongMasteryMap(), {
        'mixed.feedpak::0': { mastery: null, instrument: 'fretted' },
        'mixed.feedpak::1': { mastery: null, instrument: 'keys' },
    });
});

test('aggregateMasteryByInstrument computes averages and medians by authoritative classifier', () => {
    const mod = freshPlugin();
    assert.deepEqual(mod.aggregateMasteryByInstrument({
        'a.feedpak::0': { mastery: 40, instrument: 'fretted' },
        'b.feedpak::0': { mastery: 80, instrument: 'fretted' },
        'c.feedpak::0': { mastery: 75, instrument: 'keys' },
    }), [
        { instrument: 'fretted', label: 'Fretted', count: 2, average: 60, median: 60 },
        { instrument: 'keys', label: 'Keys', count: 1, average: 75, median: 75 },
    ]);
});

test('aggregateMasteryByInstrument excludes null, legacy, and unsupported records', () => {
    const mod = freshPlugin();
    assert.deepEqual(mod.aggregateMasteryByInstrument({
        'pending.feedpak::0': { mastery: null, instrument: 'keys' },
        'legacy.feedpak::0': 70,
        'drums.feedpak::0': { mastery: 90, instrument: 'drums' },
        'keys.feedpak::0': { mastery: 120, instrument: 'keys' },
    }), [
        { instrument: 'keys', label: 'Keys', count: 1, average: 100, median: 100 },
    ]);
});

test('aggregateMasteryByInstrument returns no groups for malformed or empty maps', () => {
    const mod = freshPlugin();
    assert.deepEqual(mod.aggregateMasteryByInstrument(null), []);
    assert.deepEqual(mod.aggregateMasteryByInstrument([]), []);
    assert.deepEqual(mod.aggregateMasteryByInstrument({}), []);
});

test('renderProfileBaseline injects a read-only card after the core best-scores card', () => {
    const mod = freshPlugin();
    mod.saveSongMasteryMap({
        'lead.feedpak::0': { mastery: 64, instrument: 'fretted' },
        'keys.feedpak::0': { mastery: 82, instrument: 'keys' },
    });
    let inserted = null;
    const anchor = { insertAdjacentElement: (where, node) => { assert.equal(where, 'afterend'); inserted = node; } };
    function element(tag) {
        return {
            tag, children: [], style: {},
            appendChild(child) { this.children.push(child); },
            remove() {},
        };
    }
    global.document = {
        getElementById(id) {
            if (id === 'v3-profile-bests') return { parentElement: anchor };
            return null;
        },
        createElement: element,
    };

    mod.renderProfileBaseline();
    assert.equal(inserted.id, 'difficulty-ladder-profile-baseline');
    assert.equal(inserted.children[0].textContent, 'Adaptive difficulty baseline');
    assert.equal(inserted.children[2].children.length, 2);
    assert.equal(inserted.children[2].children[0].children[0].children[1].textContent, '64%');
    assert.equal(inserted.children[2].children[1].children[0].children[1].textContent, '82%');
});

test('renderProfileBaseline stays absent when no classified mastery exists', () => {
    const mod = freshPlugin();
    let inserted = null;
    const anchor = { insertAdjacentElement: (_where, node) => { inserted = node; } };
    global.document = {
        getElementById(id) {
            if (id === 'v3-profile-bests') return { parentElement: anchor };
            return null;
        },
    };

    mod.renderProfileBaseline();

    assert.equal(inserted, null);
});

test('renderProfileBaseline aggregates from the active player\'s v2 progress, not just the legacy v1 map', () => {
    const mod = freshPlugin();
    const ctx = playerContext({ player_id: 'main', instrument: 'guitar', role: 'lead', song_id: 'lead.feedpak' });
    mod.upsertPlayerContext(ctx);
    // Live difficulty changes write only to v2 now — the v1 map is empty.
    mod.writeProgress(ctx, { currentDifficulty: 64 });

    let inserted = null;
    const anchor = { insertAdjacentElement: (_where, node) => { inserted = node; } };
    function element(tag) {
        return { tag, children: [], style: {}, appendChild(child) { this.children.push(child); }, remove() {} };
    }
    global.document = {
        getElementById(id) { return id === 'v3-profile-bests' ? { parentElement: anchor } : null; },
        createElement: element,
    };

    mod.renderProfileBaseline();

    assert.notEqual(inserted, null, 'a v2-only profile must still produce the baseline card');
    assert.equal(inserted.children[2].children[0].children[0].children[1].textContent, '64%');
});

// ── Auto-adjust warm-up window + ramped stepping ────────────────────────────
// (Rocksmith-comparison audit follow-up: a fresh song no longer acts before
// WARMUP_PHRASES phrases are scored, and a qualifying streak now ramps
// mastery by rampStep() per phrase instead of jumping the full th.step in
// one call — see ROCKSMITH_COMPARISON.md.)

function attachHighwayStub(initialPct) {
    let masteryFrac = initialPct / 100;
    const calls = [];
    global.window.highway = { getMastery: () => masteryFrac };
    global.window.setMastery = (pct) => { calls.push(pct); masteryFrac = pct / 100; };
    return calls;
}

function setDropResistance(value) {
    global.localStorage.setItem('difficulty_ladder.dropResistance', JSON.stringify(value));
    global.window.dispatchEvent({
        type: 'difficulty_ladder:settings-changed',
        detail: { dropResistance: value },
    });
}

test('rampStep() increments total the exact full step at every sensitivity', () => {
    const mod = freshPlugin();
    mod.settings.sensitivity = 1;
    assert.deepEqual([0, 1, 2].map(i => mod.rampStep(mod.thresholds(), i)), [3, 4, 3]);
    mod.settings.sensitivity = 2;
    assert.deepEqual([0, 1, 2].map(i => mod.rampStep(mod.thresholds(), i)), [5, 5, 5]);
    mod.settings.sensitivity = 3;
    assert.deepEqual([0, 1, 2].map(i => mod.rampStep(mod.thresholds(), i)), [7, 6, 7]);
});

test('rampStep() never returns less than 1', () => {
    const mod = freshPlugin();
    assert.equal(mod.rampStep({ step: 1 }), 1);
    assert.equal(mod.rampStep({ step: 0 }), 1);
});

test('down-step ratio scales only the final downward target and preserves the ramp', () => {
    const mod = freshPlugin();
    mod.settings.downStepRatio = 1.5;
    const th = { step: 15 };
    const up = [0, 1, 2].map(i => mod.rampStep(th, i, 'up'));
    const down = [0, 1, 2].map(i => mod.rampStep(th, i, 'down'));
    assert.deepEqual(up, [5, 5, 5]);
    assert.equal(down.reduce((sum, step) => sum + step, 0), 23);
    assert.ok(Math.max(...down) - Math.min(...down) <= 1);
});

test('down-step ratio clamps malformed and out-of-range settings', () => {
    const mod = freshPlugin();
    mod.settings.downStepRatio = 99;
    assert.equal(mod.downStepRatio(), 2);
    mod.settings.downStepRatio = 'bad';
    assert.equal(mod.downStepRatio(), 1);
});

test('commitPhraseResult() does not act before WARMUP_PHRASES phrases have been scored', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 2;
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES - 1; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls.length, 0, 'no auto-adjust call before warm-up is satisfied');
    mod.commitPhraseResult(1.0); // the WARMUP_PHRASES-th qualifying phrase
    assert.equal(calls.length, 1);
});

test('warm-up phrases scored while autoAdjust is off still count toward WARMUP_PHRASES', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = false;
    mod.settings.sensitivity = 2;
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls.length, 0, 'autoAdjust was off — nothing should have been applied');
    mod.settings.autoAdjust = true;
    mod.commitPhraseResult(1.0);
    assert.equal(calls.length, 1, 'warm-up was already satisfied while paused');
});

test('mastery streak activates after three accurate phrases at configured max', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = false;
    mod.settings.maxMastery = 80;
    attachHighwayStub(80);
    for (let i = 1; i <= mod.MASTERY_STREAK_PHRASES; i++) {
        mod.commitPhraseResult(mod.MASTERY_STREAK_ACCURACY);
        assert.deepEqual(mod.masteryStreakStatus(), {
            count: i,
            active: i >= mod.MASTERY_STREAK_PHRASES,
        });
    }
});

test('mastery streak resets below the accuracy floor or configured max', () => {
    const mod = freshPlugin();
    mod.settings.maxMastery = 100;
    assert.equal(mod.updateMasteryStreak(1, 100), 1);
    assert.equal(mod.updateMasteryStreak(mod.MASTERY_STREAK_ACCURACY - 0.01, 100), 0);
    assert.equal(mod.updateMasteryStreak(1, 99), 0);
});

test('mastery streak reset helper models a pause without changing scoring state', () => {
    const mod = freshPlugin();
    mod.updateMasteryStreak(1, 100);
    mod.updateMasteryStreak(1, 100);
    mod.resetMasteryStreak();
    assert.deepEqual(mod.masteryStreakStatus(), { count: 0, active: false });
});

test('mastery lifecycle subscriptions reset while active and detach while hidden', () => {
    const mod = freshPlugin();
    const handlers = new Map();
    global.window.feedBack = {
        on(eventName, handler) {
            handlers.set(eventName, handler);
            return () => handlers.delete(eventName);
        },
    };
    mod.startMasteryLifecycleSubscriptions();
    assert.deepEqual([...handlers.keys()], ['song:pause', 'song:stop', 'song:ended']);

    mod.updateMasteryStreak(1, 100);
    handlers.get('song:pause')();
    assert.deepEqual(mod.masteryStreakStatus(), { count: 0, active: false });

    mod.stopMasteryLifecycleSubscriptions();
    assert.equal(handlers.size, 0);
});

test('Split Screen scoring state is isolated and changes only its own panel highway', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 2;
    const stateA = mod.newSplitScoreState();
    const stateB = mod.newSplitScoreState();
    let masteryA = 0.50;
    let masteryB = 0.50;
    const highwayA = { getMastery: () => masteryA, setMastery: (v) => { masteryA = v; } };
    const highwayB = { getMastery: () => masteryB, setMastery: (v) => { masteryB = v; } };

    // Each panel needs its own warm-up; A's second result must not warm B up.
    mod.commitSplitPhraseResult(stateA, highwayA, 1);
    mod.commitSplitPhraseResult(stateB, highwayB, 1);
    mod.commitSplitPhraseResult(stateA, highwayA, 1);

    assert.equal(masteryA, 0.55); // first third of the default 15-point ramp
    assert.equal(masteryB, 0.50);
});

test('qualifying streaks total the configured step for every sensitivity and direction', () => {
    for (const sensitivity of [1, 2, 3]) {
        for (const [ratio, sign] of [[1.0, 1], [0.0, -1]]) {
            const mod = freshPlugin();
            mod.settings.autoAdjust = true;
            mod.settings.sensitivity = sensitivity;
            const calls = attachHighwayStub(50);
            const th = mod.thresholds();
            for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(ratio);
            for (let i = 1; i < mod.RAMP_PHRASES; i++) mod.commitPhraseResult(ratio);
            assert.equal(calls.length, mod.RAMP_PHRASES);
            assert.equal(calls[calls.length - 1] - 50, sign * th.step,
                `sensitivity ${sensitivity}, direction ${sign > 0 ? 'up' : 'down'}`);
        }
    }
});

test('qualifying downward streak uses the configured asymmetric target', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 2;
    mod.settings.downStepRatio = 1.5;
    const calls = attachHighwayStub(75);
    for (let i = 0; i < mod.WARMUP_PHRASES + mod.RAMP_PHRASES - 1; i++) mod.commitPhraseResult(0);
    assert.equal(calls[calls.length - 1], 52);
});

test('Split Screen downward streak uses the configured asymmetric target', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 2;
    mod.settings.downStepRatio = 1.5;
    const state = mod.newSplitScoreState();
    let mastery = 0.75;
    const highway = {
        getMastery: () => mastery,
        setMastery: value => { mastery = value; },
    };
    for (let i = 0; i < mod.WARMUP_PHRASES + mod.RAMP_PHRASES - 1; i++)
        mod.commitSplitPhraseResult(state, highway, 0);
    assert.equal(mastery, 0.52);
});

test('dropResistance loads true only from persisted boolean true', () => {
    const key = 'difficulty_ladder.dropResistance';
    assert.equal(freshPlugin({ stored: { [key]: JSON.stringify('false') } }).settings.dropResistance, false);
    assert.equal(freshPlugin({ stored: { [key]: JSON.stringify(true) } }).settings.dropResistance, true);
});

test('malformed dropResistance storage updates reset the setting to false', () => {
    const mod = freshPlugin();
    mod.settings.dropResistance = true;

    global.window.dispatchEvent({
        type: 'storage',
        key: 'difficulty_ladder.dropResistance',
        newValue: 'not-json',
    });

    assert.equal(mod.settings.dropResistance, false);
});

// Issue #64: minMastery/maxMastery are persisted independently by the two
// settings.html number inputs, so an inverted pair (min > max) can reach
// screen.js from a stale write, a manual localStorage edit, or a race
// between tabs. The README promises auto-adjust never crosses these
// bounds; that only holds for a valid interval, since
// Math.max(min, Math.min(max, next)) returns min when min > max.

test('a valid persisted mastery range loads unchanged', () => {
    const mod = freshPlugin({ stored: {
        'difficulty_ladder.minMastery': '20',
        'difficulty_ladder.maxMastery': '80',
    } });
    assert.equal(mod.settings.minMastery, 20);
    assert.equal(mod.settings.maxMastery, 80);
});

test('an equal min/max pair loads unchanged (a valid, if degenerate, interval)', () => {
    const mod = freshPlugin({ stored: {
        'difficulty_ladder.minMastery': '50',
        'difficulty_ladder.maxMastery': '50',
    } });
    assert.equal(mod.settings.minMastery, 50);
    assert.equal(mod.settings.maxMastery, 50);
});

test('an inverted persisted mastery range is swapped back into a valid interval on load', () => {
    const mod = freshPlugin({ stored: {
        'difficulty_ladder.minMastery': '80',
        'difficulty_ladder.maxMastery': '20',
    } });
    assert.equal(mod.settings.minMastery, 20);
    assert.equal(mod.settings.maxMastery, 80);
    assert.ok(mod.settings.minMastery <= mod.settings.maxMastery);
});

test('malformed mastery bounds fall back to the full 0..100 range', () => {
    const mod = freshPlugin({ stored: {
        'difficulty_ladder.minMastery': 'not-json',
        'difficulty_ladder.maxMastery': 'not-json',
    } });
    assert.equal(mod.settings.minMastery, 0);
    assert.equal(mod.settings.maxMastery, 100);
});

test('_normalizeMasteryBounds swaps an inverted in-memory pair without discarding either configured number', () => {
    const mod = freshPlugin();
    mod.settings.minMastery = 90;
    mod.settings.maxMastery = 30;
    mod._normalizeMasteryBounds();
    assert.equal(mod.settings.minMastery, 30);
    assert.equal(mod.settings.maxMastery, 90);
});

test('a storage event that inverts the range is normalized immediately', () => {
    const mod = freshPlugin();
    mod.settings.maxMastery = 40;

    global.window.dispatchEvent({
        type: 'storage',
        key: 'difficulty_ladder.minMastery',
        newValue: JSON.stringify(60),
    });

    assert.equal(mod.settings.minMastery, 40);
    assert.equal(mod.settings.maxMastery, 60);
});

test('a settings-changed event that inverts the range is normalized immediately', () => {
    const mod = freshPlugin();
    mod.settings.minMastery = 10;

    global.window.dispatchEvent({
        type: 'difficulty_ladder:settings-changed',
        detail: { maxMastery: 5 },
    });

    assert.equal(mod.settings.minMastery, 5);
    assert.equal(mod.settings.maxMastery, 10);
});

test('auto-adjust never lands outside a subsequently-corrected mastery range', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 2;
    // Simulate the pre-fix bug directly against the clamp's inputs: an
    // inverted pair must not survive to be read by the clamp at all.
    mod.settings.minMastery = 90;
    mod.settings.maxMastery = 30;
    mod._normalizeMasteryBounds();
    const calls = attachHighwayStub(35);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(0.0);
    for (let i = 1; i < mod.RAMP_PHRASES * 3; i++) mod.commitPhraseResult(0.0);
    for (const pct of calls) {
        assert.ok(pct >= mod.settings.minMastery && pct <= mod.settings.maxMastery,
            `${pct} escaped the [${mod.settings.minMastery}, ${mod.settings.maxMastery}] bound`);
    }
});

test('auto-adjust stops ramping as soon as the signal returns to neutral (no full step committed in advance)', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 2; // th.up 0.88, th.down 0.68, default reactionSpeed -> alpha 0.35
    const calls = attachHighwayStub(50);
    const th = mod.thresholds();
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls.length, 1);
    const movedSoFar = calls[0] - 50;
    assert.ok(movedSoFar < th.step, 'a single ramped step should be smaller than a full step');
    // ratio 0.4 pulls the EMA from 1.0 to 0.35*0.4 + 0.65*1.0 = 0.79 — inside
    // the neutral band (0.68, 0.88), so no further action should be taken.
    mod.commitPhraseResult(0.4);
    assert.equal(calls.length, 1, 'no new setMastery call once the EMA is back in the neutral band');
});

test('changing ramp direction restarts at the first exact-remainder increment', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 1; // exact sequence is 3, 4, 3
    mod.settings.reactionSpeed = 3; // alpha 0.5: one miss moves EMA 1.0 -> 0.5, below th.down 0.65
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls[0], 53);
    mod.commitPhraseResult(0.0); // EMA reaches the down threshold; direction flips
    assert.equal(calls[1], 50, 'downward ramp restarts with 3 rather than continuing with 4');
});

test('per-song reset clears exact-remainder ramp progress', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 1;
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls[0], 53);
    mod.resetPerSongState();
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls[1], 56, 'new song starts with 3 rather than the prior ramp\'s next 4');
});

test('drop resistance requires two consecutive below-threshold signals when enabled', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    setDropResistance(true);
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(0.0);
    assert.equal(calls.length, 0, 'first eligible low signal is held');
    setDropResistance(true); // public settings event invalidates the pending confirmation
    assert.equal(global.localStorage.getItem('difficulty_ladder.dropResistance'), 'true');
    mod.commitPhraseResult(0.0);
    assert.equal(calls.length, 0, 'first low signal after a setting event is held again');
    mod.commitPhraseResult(0.0);
    assert.equal(calls.length, 1);
    assert.equal(calls[0], 45);
});

test('manual mastery change invalidates a resisted drop before the first auto-apply', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    setDropResistance(true);
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(0.0);
    assert.equal(calls.length, 0, 'one low signal is pending');

    global.window.highway.getMastery = () => 0.60;
    mod.commitPhraseResult(0.0);
    assert.equal(calls.length, 0, 'manual value is preserved instead of applying the stale drop');
    assert.equal(mod.settings.autoAdjust, false);
});

test('drop resistance streak resets when the rolling signal returns neutral', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    setDropResistance(true);
    mod.settings.reactionSpeed = 3;
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(0.6);
    assert.equal(calls.length, 0);
    mod.commitPhraseResult(1.0); // EMA 0.8: neutral, clears the one-signal streak
    mod.commitPhraseResult(0.0); // EMA 0.4: first new low signal
    assert.equal(calls.length, 0);
    mod.commitPhraseResult(0.0); // second consecutive low signal
    assert.equal(calls.length, 1);
});

test('drop resistance does not delay upward adjustments', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    setDropResistance(true);
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls.length, 1);
    assert.equal(calls[0], 55);
});

test('min/maxMastery still clamps a ramped next value and stops repeat calls once saturated', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 2;
    mod.settings.maxMastery = 58;
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls.length, 1);
    assert.equal(calls[0], 55); // 50 + rampStep(5), under the 58 cap
    mod.commitPhraseResult(1.0); // would ramp to 60 -> clamps to 58
    assert.equal(calls.length, 2);
    assert.equal(calls[1], 58);
    mod.commitPhraseResult(1.0); // already saturated -> next === curPct -> no call
    assert.equal(calls.length, 2);
});

test('originless programmatic mastery drift conservatively disables autoAdjust', () => {
    const mod = freshPlugin();
    mod.settings.autoAdjust = true;
    mod.settings.sensitivity = 2;
    const calls = attachHighwayStub(50);
    for (let i = 0; i < mod.WARMUP_PHRASES; i++) mod.commitPhraseResult(1.0);
    assert.equal(calls.length, 1);
    assert.equal(mod.settings.autoAdjust, true);
    // The compatibility getter exposes the changed value but no origin. Even
    // a programmatic writer therefore triggers the conservative stand-down.
    global.window.highway.getMastery = () => 0.42;
    mod.commitPhraseResult(1.0);
    assert.equal(mod.settings.autoAdjust, false);
    assert.equal(calls.length, 1, 'stood down instead of fighting originless drift');
});

// ── Generate ladder depth cap (generateLevels) ──────────────────────────────

test('currentTarget() clamps generateLevels to [2,8] and includes it in the /generate target', () => {
    const mod = freshPlugin();
    global.window.highway = { getSongInfo: () => ({ filename: 'song.feedpak', arrangement_index: 1 }) };
    mod.settings.generateLevels = 6;
    assert.deepEqual(mod.currentTarget(), { filename: 'song.feedpak', arrangement_index: 1, levels: 6 });
    mod.settings.generateLevels = 99; // out of range -> clamps to 8
    assert.equal(mod.currentTarget().levels, 8);
    mod.settings.generateLevels = 1; // out of range -> clamps to 2
    assert.equal(mod.currentTarget().levels, 2);
});

test('currentTarget() clamps a parsed 0 to 2 instead of falling back to the default 4', () => {
    const mod = freshPlugin();
    global.window.highway = { getSongInfo: () => ({ filename: 'song.feedpak', arrangement_index: 0 }) };
    mod.settings.generateLevels = 0; // `|| 4` would misfire here — 0 is a legitimate parse, not NaN
    assert.equal(mod.currentTarget().levels, 2);
});

test('currentTarget() returns null when there is no song loaded yet', () => {
    const mod = freshPlugin();
    global.window.highway = { getSongInfo: () => null };
    assert.equal(mod.currentTarget(), null);
});

// ── Cross-plugin contract shape parity (issue #8) ───────────────────────────
// Neither plugin defines this shape itself — it's window.highway's contract
// (feedBack core) — but both plugins' code assumes the same fields exist.
// This is a documentation-as-test guard: if either plugin's assumed field
// list drifts from the other's, this is the place a future editor would
// update both, rather than one silently going stale.
test('the phrase shape this plugin assumes matches the one sectionmap assumes (contract note)', () => {
    // getPhrases(): [{ index, start_time, end_time, max_difficulty }]
    const samplePhrase = { index: 0, start_time: 0, end_time: 10, max_difficulty: 3 };
    // Fields this plugin's drawHud()/tickScoring() reads:
    for (const key of ['start_time', 'end_time', 'max_difficulty']) {
        assert.ok(key in samplePhrase, `difficulty_ladder reads phrase.${key}`);
    }
    // Fields feedBack-plugin-sectionmap's _smSectionDifficulty/_smComputeGlass
    // read (see that repo's screen.js): the same three, plus none extra —
    // confirming neither plugin depends on a field the other doesn't also see.
    for (const key of ['start_time', 'end_time', 'max_difficulty']) {
        assert.ok(key in samplePhrase, `sectionmap reads phrase.${key}`);
    }
});

// ── Generate-difficulties CTA request path (PR #37 follow-up) ──────────────
// onGenerateClick() originally had two bugs that only threw at runtime,
// never in a type check: it called `setGenerateLabel(...)` before that
// function was defined anywhere in the file (ReferenceError, thrown
// *before* the try block, so it wasn't caught and the `finally` that
// clears `_generating` / `_generateBtn.disabled` never ran — the button
// locked up permanently after one click), and the post-success reconnect
// read a bare `hw` that was never declared in this function's scope
// (every other function does `var hw = window.highway;` locally) — a
// second ReferenceError, this one inside the try block, so it was
// swallowed and silently reported as "Generate failed" even though the
// backend generation had already succeeded. Both only manifested by
// actually invoking onGenerateClick(), which no prior test in this file
// did. `setGenerateLabel` is now a real, properly-defined helper (#41) —
// these tests still guard the underlying invariants (fetch is reached,
// reconnect fires, the button doesn't get stuck).

function fakeElement() {
    var el = {
        style: {}, title: '', id: '', disabled: false,
        classList: { toggle: function () {}, add: function () {}, remove: function () {} },
        appendChild: function () {},
        contains: function () { return false; },
        onclick: null,
    };
    return el;
}

function mountGenerateBtn(mod) {
    var created = [];
    global.document.createElement = function () {
        var el = fakeElement();
        created.push(el);
        return el;
    };
    var slot = fakeElement();
    global.window.feedBack = { uiVersion: 'v3', ui: { playerControlSlot: function () { return slot; } } };
    mod.mountControls();
    return created.filter(function (el) { return el.id === 'dynamic-difficulty-generate'; })[0];
}

test('onGenerateClick reaches fetch() without throwing on the way in (setGenerateLabel/hw setup)', async () => {
    const mod = freshPlugin();
    const btn = mountGenerateBtn(mod);
    global.window.highway = {
        getSongInfo: () => ({ filename: 'song.feedpak', arrangement_index: 0 }),
        hasPhraseData: () => false,
        reconnect: () => {},
    };
    let fetchCalled = false;
    global.fetch = async () => {
        fetchCalled = true;
        return { ok: true, json: async () => ({}) };
    };

    await mod.onGenerateClick();

    assert.equal(fetchCalled, true, 'a ReferenceError before the try block (originally: an undefined setGenerateLabel()) would prevent fetch() from ever running');
    assert.notEqual(btn.textContent, 'Generate failed');
});

test('onGenerateClick calls window.highway.reconnect() after a successful (non-skipped) generation', async () => {
    const mod = freshPlugin();
    const btn = mountGenerateBtn(mod);
    const reconnectCalls = [];
    global.window.highway = {
        getSongInfo: () => ({ filename: 'song.feedpak', arrangement_index: 2 }),
        hasPhraseData: () => false,
        reconnect: (filename, idx) => reconnectCalls.push([filename, idx]),
    };
    global.fetch = async () => ({ ok: true, json: async () => ({}) });

    await mod.onGenerateClick();

    assert.deepEqual(reconnectCalls, [['song.feedpak', 2]], 'a bare undeclared `hw` reference here used to throw and get reported as "Generate failed"');
    assert.notEqual(btn.textContent, 'Generate failed');
});

test('onGenerateClick releases the _generating guard after success, so a follow-up click is not permanently blocked', async () => {
    const mod = freshPlugin();
    mountGenerateBtn(mod);
    global.window.highway = {
        getSongInfo: () => ({ filename: 'song.feedpak', arrangement_index: 0 }),
        hasPhraseData: () => false,
        reconnect: () => {},
    };
    let fetchCallCount = 0;
    global.fetch = async () => {
        fetchCallCount++;
        return { ok: true, json: async () => ({}) };
    };

    await mod.onGenerateClick();
    await mod.onGenerateClick();

    assert.equal(fetchCallCount, 2, 'the pre-try ReferenceError used to skip the finally block, leaving _generating stuck true forever');
});
