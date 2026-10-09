(function () {
    'use strict';

    // Re-hydration guard (plugin-runtime-idempotent.v1 / spec §6.1): the Host
    // MAY re-run this script mid-session (e.g. the plugin set reloads). This
    // plugin is a persistent background overlay with no per-visit UI to
    // refresh (unlike a nav screen reacting to screen:changed), so a second
    // execution has nothing useful to do — without this guard it would start
    // a second parallel pair of rAF loops and duplicate every event listener
    // registered below.
    var _singleton = (window.__feedBackDynamicDifficulty = window.__feedBackDynamicDifficulty || { installed: false });
    if (_singleton.installed) return;
    _singleton.installed = true;

    var PLUGIN_ID = 'difficulty_ladder';
    var LS_PREFIX = 'difficulty_ladder.';

    // Section payload contracts (issue #156). v2 is what Section Map consumes
    // today and stays byte-for-byte as it is until that plugin is on v3;
    // v3 is the render-neutral replacement on its own event name, so a v3-only
    // consumer can never be handed a v2 payload and an un-upgraded consumer
    // never sees fields it doesn't know. See INTEGRATION.md -> "sections v3".
    const SECTIONS_SCHEMA_V2 = 'difficulty_ladder.sections.v2';
    const SECTIONS_SCHEMA_V3 = 'difficulty_ladder.sections.v3';
    const SECTIONS_EVENT_V2 = 'difficulty:sections-updated';
    const SECTIONS_EVENT_V3 = 'difficulty:sections-updated-v3';

    // Section Map's released integration probe predates this plugin's rename
    // from dynamic_difficulty.  It subscribes to our public
    // `difficulty:sections-updated` event only after seeing this capability
    // marker. Keep the compatibility surface deliberately minimal: section
    // boundaries remain the host's canonical highway.getSections() data, and
    // the event payload is indexed against that exact array.
    window._ddCapabilities = window._ddCapabilities || {};
    window._ddCapabilities.sectionDifficulty = true;
    window._ddCapabilities.playerContext = 'difficulty_ladder.player_context.v1';
    // Which section-payload schema this build emits, so a consumer can pick
    // v3 when present and fall back to v2 otherwise instead of sniffing
    // payloads. Set here, at this plugin's top-level script execution, which
    // always precedes section_map's (plugins load alphabetically and
    // difficulty_ladder < section_map) — see INTEGRATION.md -> "Transition
    // policy".
    window._ddCapabilities.sectionsSchema = SECTIONS_SCHEMA_V3;

    function lsGet(key, def) {
        let v;
        try {
            v = localStorage.getItem(`${LS_PREFIX}${key}`);
            return v === null ? def : JSON.parse(v);
        } catch (_) { return def; }
    }
    function lsSet(key, val) {
        try { localStorage.setItem(`${LS_PREFIX}${key}`, JSON.stringify(val)); } catch (_) { /* noop */ }
    }
    var _pendingSettingWrites = {};
    function lsSetDebounced(key, val) {
        if (_pendingSettingWrites[key]) clearTimeout(_pendingSettingWrites[key]);
        _pendingSettingWrites[key] = setTimeout(function () {
            delete _pendingSettingWrites[key];
            lsSet(key, val);
        }, 150);
    }
    function cancelDebouncedSettingWrite(key) {
        if (!_pendingSettingWrites[key]) return;
        clearTimeout(_pendingSettingWrites[key]);
        delete _pendingSettingWrites[key];
    }

    // Issue #158 (tier rail 3/4): the `showGlasses` setting was renamed to
    // "Show difficulty guide", stored under `showDifficultyGuide`. The new
    // key is read first; a value under the legacy key is migrated forward
    // (persisted to the new key) so an existing user's on/off choice
    // survives the upgrade instead of resetting to the default. Runs once
    // at settings-load time — never from a gameplay-event handler — and is
    // idempotent: once the new key exists the legacy key is never consulted
    // again, and a fresh install (neither key present) takes the default
    // without writing anything.
    function _resolveDifficultyGuideSetting() {
        var current = lsGet('showDifficultyGuide', null);
        if (current !== null) return current === true;
        var legacy = lsGet('showGlasses', null);
        if (legacy !== null) {
            var migrated = legacy === true;
            lsSet('showDifficultyGuide', migrated);
            return migrated;
        }
        return true;
    }

    // ---- Player-scoped progress persistence ------------------------------
    // Core only persists master_difficulty as a single global (server.py's
    // /api/settings) — switching songs mid-session keeps whatever % the
    // previous song ended on. This remembers each song's own last-used value
    // (Slopsmith's song_mastery plugin did the same, per-filename) so
    // revisiting a song you'd auto-adjusted or manually set restores where
    // you left off, instead of inheriting an unrelated song's difficulty.
    const SONG_MASTERY_LS_KEY = `${LS_PREFIX}songMastery`; // legacy, read-only at runtime
    const PHRASE_ATTEMPTS_LS_KEY = `${LS_PREFIX}phraseAttempts.v1`; // legacy migration source
    const PROGRESS_LS_KEY = `${LS_PREFIX}progress.v2`;
    const PHRASE_ATTEMPTS_V2_LS_KEY = `${LS_PREFIX}phraseAttempts.v2`;
    const PROGRESS_SCHEMA = 'difficulty_ladder.progress.v2';
    const PHRASE_ATTEMPTS_SCHEMA = 'difficulty_ladder.phrase_attempts.v2';
    const PLAYER_CONTEXT_SCHEMA = 'difficulty_ladder.player_context.v1';
    const MAX_PHRASE_ATTEMPTS = 5000;
    const PERSISTENCE_FLUSH_MS = 150;
    // A debounced write that fails (quota, storage unavailable) retries on its
    // own at PERSISTENCE_FLUSH_MS * 2^n (300, 600, 1200 ms), then gives up until
    // the next save()/markDirty() or lifecycle flush. Bounded so a quota that
    // stays full costs a handful of attempts, not a retry loop.
    const PERSISTENCE_RETRY_MAX = 3;
    const _sessionId = window.crypto?.randomUUID?.() || `session-${Date.now()}`;

    function _plainObject(value) {
        return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
    }

    // ---- Debounced localStorage store (issue #138) -----------------------
    // Every persistence store in this file — progress.v2, phraseAttempts.v2,
    // and the legacy read-only songMastery map — is this same shape: parse
    // and cache on first read, mutate the cached object in place, coalesce
    // writes behind a 150ms timer so no gameplay-event handler ever calls
    // localStorage synchronously (CLAUDE.md), and drop the cache when a
    // foreign `storage` write lands. They used to be three hand-written
    // copies of the same load/save/flush/schedule/invalidate quintet, each
    // with its own independently-shaped module-level cache/dirty/timer
    // triple; this factory is the single implementation.
    //
    // config:
    //   key       — full localStorage key
    //   empty()   — factory for a fresh store when nothing valid is on disk
    //   accept(v) — is a parsed payload usable as-is? (schema/version gate)
    //   ensure(v) — optional repair pass on a freshly-loaded store
    //   validate(v) — optional gate on save(), for callers that can hand back
    //               a value they don't own
    function makePersistenceStore(config) {
        let cache = null;
        let dirty = false;
        let timer = null;
        let retries = 0;   // consecutive failed debounced writes since the last save/success

        function load() {
            if (cache) return cache;
            var parsed;
            try { parsed = JSON.parse(localStorage.getItem(config.key) || 'null'); } catch (_) { parsed = null; }
            cache = (config.accept || _plainObject)(parsed) ? parsed : config.empty();
            if (config.ensure) config.ensure(cache);
            return cache;
        }

        // Synchronous write of the current cache. Used directly only on
        // cold paths (one-time legacy migration, immediate-save helpers);
        // returns false and leaves the store dirty so the next scheduled or
        // lifecycle flush retries it.
        function write() {
            if (!cache) return false;
            try {
                localStorage.setItem(config.key, JSON.stringify(cache));
                dirty = false;
                retries = 0;
                return true;
            } catch (_) { return false; }
        }

        // Returns whether the store is persisted: true when the write landed
        // OR when there was nothing pending to write, false only when a write
        // was attempted and failed (invalidate() keys off that).
        function flush() {
            if (timer) { clearTimeout(timer); timer = null; }
            if (!cache || !dirty) return true;
            return write();
        }

        // Debounce timer body: flush, and if the write failed retry with a
        // doubling delay up to PERSISTENCE_RETRY_MAX times. Only this timer
        // path retries — an explicit flush() (lifecycle) never re-arms itself.
        function tick() {
            timer = null;
            if (flush() === false && retries < PERSISTENCE_RETRY_MAX) {
                retries += 1;
                timer = setTimeout(tick, PERSISTENCE_FLUSH_MS * Math.pow(2, retries));
            }
        }

        // Fresh data restarts the retry budget.
        function schedule() {
            retries = 0;
            if (timer) clearTimeout(timer);
            timer = setTimeout(tick, PERSISTENCE_FLUSH_MS);
        }

        // Hand a whole new value to the store and arm the debounce.
        function save(value) {
            if (config.validate && !config.validate(value)) return false;
            cache = value;
            dirty = true;
            schedule();
            return true;
        }

        // Take ownership of a value and persist it in the same call — for
        // callers that can prove they aren't on the per-frame gameplay path.
        function saveNow(value) {
            if (config.validate && !config.validate(value)) return false;
            cache = value;
            dirty = true;
            return write();
        }

        // Callers that mutate the cached store in place (rather than handing
        // back a whole value) mark it dirty here.
        function markDirty() {
            dirty = true;
            schedule();
        }

        // A foreign tab wrote this key. Our own pending (debounced) mutation
        // lives only in the cache, so discarding it here would silently drop
        // it — flush it first, then drop the cache so the next read parses
        // the merged-by-last-write-wins reality. A store whose writes are
        // never debounced (saveNow only) passes false: there is nothing
        // pending to lose, and a retry here would only replay a write that
        // already failed over the foreign tab's value.
        //
        // If that recovery write itself fails, the cache is kept and stays
        // dirty rather than dropped: the mutation has no other holder. flush()
        // disarmed the debounce before writing, so re-arm it here — otherwise
        // an idle tab (no further writes, no song change) would hold the record
        // in memory indefinitely. schedule() fires flush() once and does not
        // loop, so a quota that stays full costs one more failed attempt, and a
        // later lifecycle flush (song change, screen hidden) still retries it.
        // The cost is that this tab's record wins over the foreign one in the
        // meantime, which is the same last-write-wins outcome the pre-refactor
        // flush-then-clear code reached whenever its write succeeded.
        function invalidate(flushPending) {
            if (dirty && flushPending !== false && flush() === false) { schedule(); return; }
            cache = null;
            dirty = false;
        }

        return { load, save, saveNow, write, flush, markDirty, invalidate };
    }

    // The legacy songMastery map remains available to the compatibility/test
    // helpers below. New runtime writes go only through progress.v2 and are
    // rejected until an explicit profile identity is ready.
    const _songMasteryStore = makePersistenceStore({
        key: SONG_MASTERY_LS_KEY,
        // typeof [] === 'object' too — an array here would make
        // map[_songKey] = pct set a non-index property that
        // JSON.stringify silently drops from array output, so per-song
        // mastery would never actually persist. _plainObject rejects arrays.
        accept: _plainObject,
        empty: function () { return {}; },
    });

    const _progressStore = makePersistenceStore({
        key: PROGRESS_LS_KEY,
        accept: function (value) {
            return _plainObject(value) && value.schema === PROGRESS_SCHEMA && _plainObject(value.profiles);
        },
        empty: function () { return { schema: PROGRESS_SCHEMA, version: 2, profiles: {}, migrations: {} }; },
        ensure: function (store) { if (!_plainObject(store.migrations)) store.migrations = {}; },
        validate: function (store) {
            return _plainObject(store) && store.schema === PROGRESS_SCHEMA && _plainObject(store.profiles);
        },
    });

    const _phraseAttemptStore = makePersistenceStore({
        key: PHRASE_ATTEMPTS_V2_LS_KEY,
        accept: function (value) {
            return _plainObject(value) && value.schema === PHRASE_ATTEMPTS_SCHEMA && _plainObject(value.profiles);
        },
        empty: function () { return { schema: PHRASE_ATTEMPTS_SCHEMA, version: 2, profiles: {}, migrations: {} }; },
        ensure: function (store) { if (!_plainObject(store.migrations)) store.migrations = {}; },
    });

    function _id(value, fallback) {
        if (value == null || String(value).trim() === '') return fallback || null;
        return String(value).trim();
    }

    // Every dynamic object key derived from context identity (profile,
    // player, song, arrangement, instrument, role, skill) in this file's
    // storage trees funnels through here. Prefixed so an identity value of
    // "__proto__", "constructor", or "prototype" — plausible from an
    // externally-supplied Host/profile/session identity, not just a local
    // user — can never collide with a plain object's own inherited
    // properties when used as `obj[key] = ...` below. This is a real
    // prototype-pollution guard, not decoration: without the prefix,
    // `store.profiles['__proto__'] = {...}` would write onto
    // Object.prototype instead of an own property. New in this PR's v2
    // schema, so there is no pre-existing on-disk key format to preserve.
    function _nodeKey(value) {
        return 'k_' + encodeURIComponent(_id(value, 'unknown'));
    }

    function _pct(value) {
        // Number(null) is 0 and Number('') is 0 — both would otherwise
        // silently coerce an unset/blank field into a real 0% value here,
        // which several callers rely on _pct(...) === null to distinguish
        // from an actually-saved 0%.
        if (value === null || value === undefined || value === '') return null;
        var parsed = typeof value === 'number' ? value : Number(value);
        return isFinite(parsed) ? Math.max(0, Math.min(100, parsed)) : null;
    }

    function _isKaraokeRole(value) {
        return /^(karaoke|vocal|vocals|singer|harmony)$/i.test(_id(value, ''));
    }

    // Persistence helpers always accept a context explicitly. The main-player
    // compatibility adapter supplies one for today's Host; Split Screen and
    // future Host versions provide one per player. No mutable global profile
    // identity is consulted here, which keeps four (or more) simultaneous
    // players isolated even when their song/arrangement happens to match.
    function normalizePlayerContext(raw, defaults) {
        raw = _plainObject(raw) || {};
        defaults = _plainObject(defaults) || {};
        var profile = _plainObject(raw.profile) || {};
        var song = _plainObject(raw.song) || {};
        var arrangement = _plainObject(raw.arrangement) || {};
        var profileId = _id(raw.profile_id ?? raw.profileId ?? profile.id ?? defaults.profile_id);
        var profileHash = _id(raw.profile_hash ?? raw.profileHash ?? raw.player_hash
            ?? profile.hash ?? profile.player_hash ?? defaults.profile_hash);
        if (raw.profile_ready === false || raw.profileReady === false || (!profileId && !profileHash)) return null;

        var role = _id(raw.role ?? raw.role_id ?? raw.roleId ?? defaults.role, 'instrumental');
        var instrument = _id(raw.instrument ?? raw.instrument_id ?? raw.instrumentId
            ?? defaults.instrument, _isKaraokeRole(role) ? 'voice' : 'legacy-unknown');
        if (_isKaraokeRole(role)) {
            role = 'karaoke';
            instrument = 'voice';
        }
        return {
            schema: PLAYER_CONTEXT_SCHEMA,
            session_id: _id(raw.session_id ?? raw.sessionId ?? defaults.session_id, _sessionId),
            player_id: _id(raw.player_id ?? raw.playerId ?? defaults.player_id, 'main'),
            profile_id: profileId,
            profile_hash: profileHash,
            song_id: _id(raw.song_id ?? raw.songId ?? raw.filename ?? song.id ?? song.filename
                ?? defaults.song_id, 'unknown-song'),
            arrangement_id: _id(raw.arrangement_id ?? raw.arrangementId ?? raw.arrangement_index
                ?? arrangement.id ?? arrangement.index ?? defaults.arrangement_id, '0'),
            instrument: instrument,
            role: role,
            skill: _id(raw.skill ?? raw.skill_key ?? raw.skillKey ?? defaults.skill, 'overall'),
            highway: raw.highway || defaults.highway || null,
            compatibility_adapter: raw.compatibility_adapter === true || defaults.compatibility_adapter === true,
        };
    }

    function playerContextKey(context) {
        var raw = _plainObject(context && context.context) || context;
        raw = _plainObject(raw) || {};
        var sessionId = _id(raw.session_id ?? raw.sessionId);
        var playerId = _id(raw.player_id ?? raw.playerId);
        return sessionId && playerId ? _nodeKey(sessionId) + '::' + _nodeKey(playerId) : null;
    }

    function persistenceContextKey(context) {
        var ctx = normalizePlayerContext(context);
        if (!ctx) return null;
        return [
            _profileKey(ctx), _nodeKey(ctx.player_id), _nodeKey(ctx.song_id), _nodeKey(ctx.arrangement_id),
            _nodeKey(ctx.instrument), _nodeKey(ctx.role), _nodeKey(ctx.skill),
        ].join('::');
    }

    function _profileKey(context) {
        var ctx = normalizePlayerContext(context);
        return ctx ? _nodeKey(ctx.profile_hash || ctx.profile_id) : null;
    }

    /* eslint-disable security/detect-object-injection --
       Every bracket access in this file's storage-tree accessor functions
       (through the matching eslint-enable below, and in the equivalent
       phrase-attempt-store functions further down) is keyed exclusively
       through _nodeKey()/_profileKey(), which prefixes every key so
       "__proto__"/"constructor"/"prototype" can never collide with a plain
       object's own inherited properties — see the comment on _nodeKey.
       eslint-plugin-security's detect-object-injection can't see that
       data-flow guarantee and flags the bracket syntax on sight; scoped to
       just these accessor functions rather than a whole-file suppression
       so a future non-_nodeKey-derived key elsewhere still gets flagged. */
    function _profilePlayerNode(profile, context, create) {
        var ctx = normalizePlayerContext(context);
        if (!ctx || !_plainObject(profile)) return null;
        if (!_plainObject(profile.players)) {
            if (!create) return null;
            profile.players = {};
        }
        var playerKey = _nodeKey(ctx.player_id);
        var player = profile.players[playerKey];
        if (!player && create) player = profile.players[playerKey] = {
            player_id: ctx.player_id, songs: {},
        };
        if (!_plainObject(player) || (!_plainObject(player.songs) && !create)) return null;
        if (!_plainObject(player.songs)) player.songs = {};
        return player;
    }

    function loadProgressStore() { return _progressStore.load(); }
    function flushProgressStore() { _progressStore.flush(); }

    // Called from writeProgress(), which itself runs from the scoring/rAF
    // path on every phrase result (currentDifficulty and bestMastery
    // updates alike) — a synchronous localStorage.setItem here would block
    // the gameplay loop. The store keeps the in-memory cache authoritative
    // immediately and coalesces the actual write behind the shared debounce.
    function saveProgressStore(store) { return _progressStore.save(store); }

    function _progressSkillNode(store, context, create) {
        var ctx = normalizePlayerContext(context);
        if (!ctx) return null;
        var profileKey = _profileKey(ctx);
        var profile = store.profiles[profileKey];
        if (!profile && create) profile = store.profiles[profileKey] = {
            profile_id: ctx.profile_id, profile_hash: ctx.profile_hash, players: {},
        };
        var player = _profilePlayerNode(profile, ctx, create);
        if (!player) return null;
        var songKey = _nodeKey(ctx.song_id), songNode = player.songs[songKey];
        if (!songNode && create) songNode = player.songs[songKey] = { song_id: ctx.song_id, arrangements: {} };
        if (!_plainObject(songNode) || !_plainObject(songNode.arrangements)) return null;
        var arrangementKey = _nodeKey(ctx.arrangement_id), arrangementNode = songNode.arrangements[arrangementKey];
        if (!arrangementNode && create) arrangementNode = songNode.arrangements[arrangementKey] = {
            arrangement_id: ctx.arrangement_id, instruments: {},
        };
        if (!_plainObject(arrangementNode) || !_plainObject(arrangementNode.instruments)) return null;
        var instrumentKey = _nodeKey(ctx.instrument), instrumentNode = arrangementNode.instruments[instrumentKey];
        if (!instrumentNode && create) instrumentNode = arrangementNode.instruments[instrumentKey] = {
            instrument: ctx.instrument, roles: {},
        };
        if (!_plainObject(instrumentNode) || !_plainObject(instrumentNode.roles)) return null;
        var roleKey = _nodeKey(ctx.role), roleNode = instrumentNode.roles[roleKey];
        if (!roleNode && create) roleNode = instrumentNode.roles[roleKey] = { role: ctx.role, skills: {} };
        if (!_plainObject(roleNode) || !_plainObject(roleNode.skills)) return null;
        var skillKey = _nodeKey(ctx.skill), skillNode = roleNode.skills[skillKey];
        if (!skillNode && create) skillNode = roleNode.skills[skillKey] = {
            skill: ctx.skill, currentDifficulty: null, bestMastery: null, updatedAt: null,
        };
        return _plainObject(skillNode) ? skillNode : null;
    }

    function _writeProgressToStore(store, context, patch) {
        var node = _progressSkillNode(store, context, true);
        if (!node || !_plainObject(patch)) return false;
        var changed = false;
        if (Object.prototype.hasOwnProperty.call(patch, 'currentDifficulty')) {
            var difficulty = _pct(patch.currentDifficulty);
            if (difficulty !== null && node.currentDifficulty !== difficulty) {
                node.currentDifficulty = difficulty; changed = true;
            }
        }
        if (Object.prototype.hasOwnProperty.call(patch, 'bestMastery')) {
            var mastery = _pct(patch.bestMastery);
            var previousBest = _pct(node.bestMastery);
            if (mastery !== null && (previousBest === null || mastery > previousBest)) {
                node.bestMastery = mastery; changed = true;
            }
        }
        if (patch.legacyUnscoped === true && node.legacyUnscoped !== true) {
            node.legacyUnscoped = true; changed = true;
        }
        if (patch.legacy_claim_player_id
            && node.legacy_claim_player_id !== patch.legacy_claim_player_id) {
            node.legacy_claim_player_id = patch.legacy_claim_player_id; changed = true;
        }
        if (changed) node.updatedAt = patch.updatedAt || new Date().toISOString();
        return changed;
    }

    function writeProgress(context, patch) {
        if (!normalizePlayerContext(context)) return false;
        var store = loadProgressStore();
        return _writeProgressToStore(store, context, patch) ? saveProgressStore(store) : true;
    }

    function _readExactProgress(store, context) {
        return _progressSkillNode(store, context, false);
    }

    function readProgress(context, options) {
        var ctx = normalizePlayerContext(context);
        if (!ctx) return null;
        var store = loadProgressStore();
        var exact = _readExactProgress(store, ctx);
        if (exact) return exact;
        if ((!options || options.overallFallback !== false) && ctx.skill !== 'overall') {
            exact = _readExactProgress(store, Object.assign({}, ctx, { skill: 'overall' }));
            if (exact) return exact;
        }
        // Numeric v1 records had no instrument/role identity. A uniquely
        // marked unscoped migration may seed any normal context for the same
        // profile/song/arrangement; scoped records never cross-read.
        var profile = store.profiles[_profileKey(ctx)];
        var player = _profilePlayerNode(profile, ctx, false);
        var songNode = player && player.songs[_nodeKey(ctx.song_id)];
        var arrangementNode = songNode && songNode.arrangements && songNode.arrangements[_nodeKey(ctx.arrangement_id)];
        var matches = [];
        var instruments = arrangementNode && arrangementNode.instruments;
        if (_plainObject(instruments)) Object.keys(instruments).forEach(function (ik) {
            var instrumentNode = instruments[ik];
            // A migrated legacy record with a known source instrument (e.g.
            // "keys") is scoped to that instrument's own node here, even
            // though it's marked legacyUnscoped for its missing role — only
            // a record whose instrument itself was unknown at migration
            // time (the 'legacy-unknown' sentinel) should be eligible to
            // seed an arbitrary instrument's context, otherwise e.g. a
            // guitar context could inherit another player's keys progress.
            var instrumentMatches = ik === _nodeKey(ctx.instrument)
                || (instrumentNode && instrumentNode.instrument === 'legacy-unknown');
            if (!instrumentMatches) return;
            var roles = instrumentNode && instrumentNode.roles;
            if (!_plainObject(roles)) return;
            Object.keys(roles).forEach(function (rk) {
                var skills = roles[rk] && roles[rk].skills;
                var node = skills && (skills[_nodeKey(ctx.skill)] || skills[_nodeKey('overall')]);
                if (_plainObject(node) && node.legacyUnscoped === true
                    && node.legacy_claim_player_id === ctx.player_id) matches.push(node);
            });
        });
        return matches.length === 1 ? matches[0] : null;
    }
    /* eslint-enable security/detect-object-injection */

    function loadSongMasteryMap() { return _songMasteryStore.load(); }
    // Legacy map: runtime reads are non-hot (song load, library badge), so
    // its save persists immediately rather than through the debounce every
    // other store shares. Deliberately returns nothing, as it always has —
    // no caller can act on the outcome, and the store's boolean would read as
    // a new contract.
    function saveSongMasteryMap(map) { _songMasteryStore.saveNow(map); }
    function _masteryPct(record) {
        let value;
        value = record && typeof record === 'object' ? record.mastery : record;
        return (typeof value === 'number' && isFinite(value)) ? value : null;
    }
    // key is always songKeyOf()'s `filename + '::' + arrangementKey` — the
    // literal '::' substring means it can never equal a dangerous prototype
    // name ("__proto__"/"constructor"/"prototype"), so map[key] below can't
    // reach Object.prototype. eslint-plugin-security's detect-object-injection
    // can't verify that shape guarantee and flags the bracket syntax anyway.
    /* eslint-disable security/detect-object-injection */
    function _rememberSongInstrument(key, instrument) {
        if (!key || !instrument) return;
        var map = loadSongMasteryMap();
        var pct = _masteryPct(map[key]);
        if (map[key] && typeof map[key] === 'object' && map[key].instrument === instrument) return;
        map[key] = { mastery: pct, instrument: instrument };
        saveSongMasteryMap(map);
    }
    /* eslint-enable security/detect-object-injection */

    // Mirrors routes.py's _instrument_kind() — the generator-eligibility
    // classifier ('fretted'/'keys'/'drums'/'unsupported') — for the one
    // caller below that needs to know whether the backend would generate
    // a ladder for this arrangement. This is a faithful port, not an
    // independent reimplementation; the two are kept in sync via the
    // shared fixture in tests/fixtures/instrument_kind_cases.json (see
    // tests/test_dd_generation.py and tests/screen.test.js) rather than by
    // hand — do not edit this function without updating that fixture and
    // re-running both suites. The WebSocket calls the field
    // arrangement_type because its top-level `type` is the message
    // discriminator.
    //
    // NOT the only instrument classifier in this file: _songContextFields()
    // below runs a separate, differently-scoped classification (player-
    // context/mastery-profile identity — vocabulary 'guitar'/'keys'/'voice'/
    // 'legacy-unknown', no name-sniffing fallback) for an unrelated purpose.
    // The two can and do disagree on the same input (e.g. a blank-type
    // "Vocals" arrangement is 'unsupported' here, 'voice' there) — this
    // fixture covers only this function, not that one.
    function _instrumentKind(arrType, arrName) {
        var t = typeof arrType === 'string' ? arrType.trim().toLowerCase() : '';
        var n = typeof arrName === 'string' ? arrName.trim() : '';

        // Explicit type always takes precedence.
        if (t === 'drums' || t === 'drum') return 'drums';
        if (t === 'piano' || t === 'keys') return 'keys';

        // When type is blank, name-sniff for unsupported instruments and
        // keys before defaulting to fretted (feedpakr omits `type` for
        // fretted/keys, so blank-type is the common case, not the edge one).
        if (t === '') {
            if (/^(keys|piano|keyboard|synth)/i.test(n)) return 'keys';
            if (/^(drums?|percussion|kit)/i.test(n)) return 'drums';
            if (/^(sax|saxophone|vocals?|voices?|violin|cello|flute|trumpet|trombone|lyrics?|notation)/i.test(n)) return 'unsupported';
            return 'fretted';
        }

        // Non-blank type: explicit allowlist.
        if (['lead', 'rhythm', 'bass', 'combo', 'chord', 'humstrum'].indexOf(t) !== -1) return 'fretted';
        return 'unsupported';
    }

    // ---- Stale-label repair (issue #141) ----
    // The repair side of the classifier fix above, which is prevent-only: a
    // label already written under the old verdict is never rewritten, because
    // _rememberSongInstrument() fires solely for 'fretted'/'keys' and there is
    // no clearing path at all. A record the old classifier stored as 'fretted'
    // (blank-type "Vocals"/"Drums") or 'keys' (any type whose name started
    // with a keys word) therefore survives forever, and the one-shot
    // songMasteryV1 migration bakes a v1 'fretted' into a durable v2 'guitar'
    // node, so the stale label outlives the v1 record it came from.
    //
    // Repair is on-contact, not a sweep: a v1 key is
    // `filename::arrangement` and carries no type or name, so the only things
    // that know what an arrangement actually is are the loaded song's metadata
    // (onSongEvent) and the backend's own per-arrangement rows
    // (rememberGeneratedInstruments). Both call in with the current verdict.
    //
    // Every key read/written below is either songKeyOf()'s
    // `filename + '::' + arrangementKey` — the literal '::' means it can never
    // equal "__proto__"/"constructor"/"prototype" — or _nodeKey()/_profileKey()-
    // derived, so no bracket access can reach an inherited property.
    /* eslint-disable security/detect-object-injection */
    function _clearStaleSongInstrument(key) {
        if (!key) return;
        var map = loadSongMasteryMap();
        var record = _plainObject(map[key]);
        // Only a record carrying an eligibility label can be a stale one: a
        // bare numeric legacy record makes no instrument claim to contradict,
        // and the migrator maps it to 'legacy-unknown' rather than to a group.
        if (record && (record.instrument === 'fretted' || record.instrument === 'keys')) {
            delete map[key];
            saveSongMasteryMap(map);
        }
        // Deliberately not gated on the v1 record: the migrated v2 node is
        // durable on its own, so requiring the clearable v1 record to still be
        // there would strand it — a v1 record that went missing (cleared here
        // on a visit whose v2 write was lost to a closed tab, a partial
        // storage wipe) can never come back to re-trigger this.
        _clearMigratedMasteryNode(key);
    }
    /* eslint-enable security/detect-object-injection */

    // The v1 record is the aggregate's fallback source, but the v2 tree is
    // what the Profile baseline prefers, so clearing v1 alone would leave the
    // stale label on screen for every install that has already migrated. The
    // migration records the profile that claimed it, and stamps every node it
    // writes with legacy_claim_player_id, so the node is findable under
    // (claimed_by, claimed_player_id) and identifiable as migration-written.
    //
    // The stamp is not proof that the whole node is migration-owned: the
    // migration also stamps a pre-existing node whose currentDifficulty was
    // still unset, and that node can hold a live bestMastery. So the repair
    // splits the two fields. bestMastery is written only by live scoring (the
    // migration never writes it), so it is the player's and is always kept.
    // currentDifficulty is the field the stale label feeds, and for an
    // arrangement the backend will not ladder it is a difficulty the plugin
    // will never apply anyway, so it is dropped either way — on a node that
    // also has played mastery that means the node is kept with its difficulty
    // cleared, and on a migration-only node the node goes with it.
    /* eslint-disable security/detect-object-injection */
    function _clearMigratedMasteryNode(key) {
        var identity = _legacySongIdentity(key);
        if (!identity) return;
        var store = loadProgressStore();
        var migration = _plainObject(store.migrations.songMasteryV1);
        if (!migration) return; // never migrated: nothing was baked into v2
        var profile = _plainObject(store.profiles[migration.claimed_by]);
        var player = profile && _plainObject(profile.players)
            ? profile.players[_nodeKey(migration.claimed_player_id)] : null;
        var songs = _plainObject(player) ? player.songs : null;
        var song = songs ? songs[_nodeKey(identity.song_id)] : null;
        var arrangements = _plainObject(song) ? song.arrangements : null;
        var arrangement = arrangements ? arrangements[_nodeKey(identity.arrangement_id)] : null;
        var instruments = _plainObject(arrangement) ? arrangement.instruments : null;
        if (!instruments) return;
        var cleared = false;
        // A v1 key is 1:1 with (song, arrangement) and the migration writes at
        // most one node per key, so under one arrangement every node the
        // migration stamped is a stale one. Which instrument it landed on is
        // not re-derived from the old label — that label is not carried into
        // the node — but the node's own instrument value tells us, so both
        // gates below read off data already in the store.
        Object.keys(instruments).forEach(function (instrumentKey) {
            var instrumentNode = instruments[instrumentKey];
            // Same gate as the v1 side above, on the migrated value: only a
            // 'fretted' or 'keys' label migrates into 'guitar' or 'keys'. A
            // bare numeric record migrates into 'legacy-unknown', which is not
            // a claim the verdict can contradict — and it never reaches the
            // Profile aggregate (_v2MasteryMapForBaseline maps only those two)
            // or the library badge, so deleting it would lose a real value that
            // nothing else can restore.
            if (!_plainObject(instrumentNode)
                || (instrumentNode.instrument !== 'guitar' && instrumentNode.instrument !== 'keys')) return;
            var roles = instrumentNode.roles;
            if (!_plainObject(roles)) return;
            Object.keys(roles).forEach(function (roleKey) {
                var skills = _plainObject(roles[roleKey]) && roles[roleKey].skills;
                if (!_plainObject(skills)) return;
                Object.keys(skills).forEach(function (skillKey) {
                    var node = _plainObject(skills[skillKey]);
                    if (!node || !_id(node.legacy_claim_player_id)) return;
                    if (_pct(node.bestMastery) === null) {
                        delete skills[skillKey];
                    } else if (_pct(node.currentDifficulty) !== null) {
                        // Guarded on the field actually being set: the node is
                        // kept, and a later visit of the same un-laddered song
                        // would otherwise re-null an already-null difficulty
                        // and count as a repair, turning a one-shot write into
                        // a full-store rewrite on every song load.
                        node.currentDifficulty = null;
                        node.updatedAt = new Date().toISOString();
                    } else return;
                    cleared = true;
                });
                if (!Object.keys(skills).length) delete roles[roleKey];
            });
            if (!Object.keys(roles).length) delete instruments[instrumentKey];
        });
        if (!cleared) return;
        // Leave the tree shaped as it was before the migration reached this
        // arrangement rather than a chain of emptied husks.
        if (!Object.keys(instruments).length) {
            delete arrangements[_nodeKey(identity.arrangement_id)];
            if (!Object.keys(arrangements).length) delete songs[_nodeKey(identity.song_id)];
        }
        saveProgressStore(store);
        // One-shot repair, not a hot gameplay path: persist immediately so a
        // tab closed right after a song load can't leave the v1 record cleared
        // while this write is still only in the 150ms debounce.
        flushProgressStore();
    }
    /* eslint-enable security/detect-object-injection */

    // ---- Library card badge (issue #4) ----
    // Surfaces the songMastery map above as a library-card decoration via the
    // Host's registration API (window.feedBack.libraryCardActions) — never a
    // MutationObserver on library DOM (CLAUDE.md's performance section calls
    // that anti-pattern out explicitly; this is exactly the extension point
    // the capability exists to replace it with).
    //
    // Known limitation (documented, not silently worked around): the
    // registered action's `label`/`icon` are static strings fixed at
    // register()-time — core's `list(song)` returns the SAME label/icon for
    // every card the predicate applies to, with no per-song text hook in this
    // capability's v1 shape (checked static/capabilities/library-card-actions.js
    // and static/v3/songs.js's songCard() rendering — `label`/`icon` come from
    // the registered spec object itself, not a value computed per `song`).
    // A literal "badge showing this song's exact N%" therefore isn't
    // expressible through `libraryCardActions` as it exists today; this
    // registers an applies()-gated indicator (visible only on cards that HAVE
    // a remembered difficulty) with the exact percentage in its title/aria
    // label, and a generic glyph otherwise — the closest faithful
    // approximation, with the exact-text gap filed as a follow-up.
    // ak/ik/rk below are Object.keys() of already-enumerated store nodes —
    // reads of own properties already discovered, not externally-chosen
    // keys — plus every other bracket access is _nodeKey()/_profileKey()-derived.
    /* eslint-disable security/detect-object-injection */
    function _dominantSongMastery(song) {
        if (!song || !song.filename) return null;
        if (_mainPlayerContext) {
            var ctx = Object.assign({}, _mainPlayerContext, { song_id: song.filename, skill: 'overall' });
            var profile = loadProgressStore().profiles[_profileKey(ctx)];
            var player = _profilePlayerNode(profile, ctx, false);
            var songNode = player && player.songs[_nodeKey(song.filename)];
            var fallbackV2 = null;
            var arrangements = songNode && songNode.arrangements;
            if (_plainObject(arrangements)) {
                Object.keys(arrangements).forEach(function (ak) {
                    var instruments = arrangements[ak] && arrangements[ak].instruments;
                    if (!_plainObject(instruments)) return;
                    Object.keys(instruments).forEach(function (ik) {
                        var roles = instruments[ik] && instruments[ik].roles;
                        if (!_plainObject(roles)) return;
                        Object.keys(roles).forEach(function (rk) {
                            var roleNode = _plainObject(roles[rk]);
                            var skills = roleNode && _plainObject(roleNode.skills);
                            var node = skills && skills[_nodeKey('overall')];
                            var value = node && _pct(node.currentDifficulty);
                            if (value === null) return;
                            if (arrangements[ak].arrangement_id === '0') fallbackV2 = value;
                            else if (fallbackV2 === null) fallbackV2 = value;
                        });
                    });
                });
            }
            return fallbackV2;
        }
        if (_profileApisPresent()) return null;
        var map = loadSongMasteryMap();
        var prefix = song.filename + '::';
        var fallback = null;
        for (var k in map) {
            if (!Object.prototype.hasOwnProperty.call(map, k) || k.indexOf(prefix) !== 0) continue;
            var v = _masteryPct(map[k]);
            if (v === null) continue;
            if (k === prefix + '0') return v; // prefer the first/primary arrangement
            if (fallback === null) fallback = v;
        }
        return fallback;
    }
    /* eslint-enable security/detect-object-injection */

    function registerLibraryCardBadge() {
        var fb = window.feedBack;
        if (!fb || !fb.libraryCardActions || typeof fb.libraryCardActions.register !== 'function') return;
        if (window.__ddCardBadgeRegistered) return; // idempotent — see plugin-runtime-idempotent.v1 guard at top of file
        window.__ddCardBadgeRegistered = true;
        fb.libraryCardActions.register({
            id: 'difficulty_ladder.mastery_badge',
            pluginId: PLUGIN_ID,
            label: 'Last played at a remembered difficulty (see this song\'s card menu for the exact %)',
            icon: '🥃',
            placement: 'overlay',
            order: 90,
            // O(1)/allocation-light per card, per the capability's own contract
            // (list(song) runs this once per visible card on every re-render).
            applies: function (song) { return _dominantSongMastery(song) !== null; },
            // Purely informational — nothing to run. Re-affirms the saved value
            // so a click is harmless rather than surprising.
            run: function (song) {
                var pct = _dominantSongMastery(song);
                return { ok: true, mastery: pct };
            },
        });
    }

    // Live difficulty changes since this PR write only to the v2 progress
    // store — loadSongMasteryMap() (the v1 map) is legacy/read-only. Reading
    // v1 alone here would leave the Profile baseline card frozen for
    // existing users and permanently empty for anyone who only ever played
    // under v2. Walks the active player's own v2 tree across every
    // song/arrangement/instrument/role/skill and projects it into the same
    // { [key]: { mastery, instrument: 'fretted'|'keys' } } shape
    // aggregateMasteryByInstrument() already expects from the v1 map, so
    // both sources can feed the same aggregator.
    /* eslint-disable security/detect-object-injection --
       every key walked below is from Object.keys() of an already-enumerated
       store node — a read of a just-discovered own property, not an
       externally-chosen key. */
    function _v2MasteryMapForBaseline(context) {
        var ctx = normalizePlayerContext(context);
        var out = {};
        if (!ctx) return out;
        var store = loadProgressStore();
        var profile = store.profiles[_profileKey(ctx)];
        var player = _profilePlayerNode(profile, ctx, false);
        var songs = player && player.songs;
        if (!_plainObject(songs)) return out;
        var n = 0;
        Object.keys(songs).forEach(function (songKey) {
            var arrangements = songs[songKey] && songs[songKey].arrangements;
            if (!_plainObject(arrangements)) return;
            Object.keys(arrangements).forEach(function (arrKey) {
                var instruments = arrangements[arrKey] && arrangements[arrKey].instruments;
                if (!_plainObject(instruments)) return;
                Object.keys(instruments).forEach(function (instrKey) {
                    var instrumentNode = instruments[instrKey];
                    var mapped = instrumentNode && instrumentNode.instrument === 'guitar' ? 'fretted'
                        : instrumentNode && instrumentNode.instrument === 'keys' ? 'keys' : null;
                    if (!mapped) return;
                    var roles = instrumentNode.roles;
                    if (!_plainObject(roles)) return;
                    Object.keys(roles).forEach(function (roleKey) {
                        var skills = roles[roleKey] && roles[roleKey].skills;
                        if (!_plainObject(skills)) return;
                        Object.keys(skills).forEach(function (skillKey) {
                            var value = _pct(skills[skillKey] && skills[skillKey].currentDifficulty);
                            if (value === null) return;
                            out['v2:' + (n++)] = { mastery: value, instrument: mapped };
                        });
                    });
                });
            });
        });
        return out;
    }
    /* eslint-enable security/detect-object-injection */

    // ---- Profile instrument baseline (issue #23) ----
    // The persisted classifier intentionally uses the generator's vocabulary
    // (`fretted` / `keys`). Keep the Profile aggregation on that authoritative
    // value rather than guessing guitar vs bass from filenames.
    function aggregateMasteryByInstrument(map) {
        var values = { fretted: [], keys: [] };
        var source = map && typeof map === 'object' && !Array.isArray(map) ? map : {};
        Object.entries(source).forEach(function (entry) {
            var record = entry[1];
            if (!record || typeof record !== 'object') return;
            var instrument = record.instrument;
            if (instrument !== 'fretted' && instrument !== 'keys') return;
            var mastery = _masteryPct(record);
            if (mastery === null) return;
            (instrument === 'keys' ? values.keys : values.fretted)
                .push(Math.max(0, Math.min(100, mastery)));
        });
        return ['fretted', 'keys'].reduce(function (groups, instrument) {
            var rows = (instrument === 'keys' ? values.keys : values.fretted)
                .sort(function (a, b) { return a - b; });
            if (!rows.length) return groups;
            var mid = Math.floor(rows.length / 2);
            var middle = rows.slice(rows.length % 2 ? mid : mid - 1, mid + 1);
            var median = middle.reduce(function (sum, value) { return sum + value; }, 0) / middle.length;
            groups.push({
                instrument: instrument,
                label: instrument === 'keys' ? 'Keys' : 'Fretted',
                count: rows.length,
                average: rows.reduce(function (sum, value) { return sum + value; }, 0) / rows.length,
                median: median,
            });
            return groups;
        }, []);
    }

    function renderProfileBaseline() {
        var v2Map = _mainPlayerContext ? _v2MasteryMapForBaseline(_mainPlayerContext) : {};
        var source = Object.keys(v2Map).length ? v2Map : loadSongMasteryMap();
        var groups = aggregateMasteryByInstrument(source);
        var previous = document.getElementById('difficulty-ladder-profile-baseline');
        if (previous) previous.remove();
        if (!groups.length) return; // Profile's absent-not-empty convention

        var bests = document.getElementById('v3-profile-bests');
        // v3-profile-bests is the content node inside the complete core card;
        // insert after that parent so this becomes a sibling in .space-y-6.
        var bestsCard = bests && bests.parentElement;
        if (!bestsCard || typeof bestsCard.insertAdjacentElement !== 'function') return;

        var card = document.createElement('section');
        card.id = 'difficulty-ladder-profile-baseline';
        card.className = 'bg-fb-card/80 backdrop-blur rounded-xl p-6 border border-fb-border/50';
        var heading = document.createElement('h3');
        heading.className = 'text-lg font-bold text-fb-text mb-1';
        heading.textContent = 'Adaptive difficulty baseline';
        card.appendChild(heading);
        var intro = document.createElement('p');
        intro.className = 'text-sm text-fb-textDim mb-4';
        intro.textContent = 'Your remembered difficulty across played arrangements. Informational only.';
        card.appendChild(intro);
        var grid = document.createElement('div');
        grid.className = 'grid grid-cols-1 sm:grid-cols-2 gap-3';
        groups.forEach(function (group) {
            var panel = document.createElement('div');
            panel.className = 'rounded-lg border border-fb-border/50 bg-fb-bg/30 p-4';
            var title = document.createElement('div');
            title.className = 'flex items-baseline justify-between gap-3';
            var label = document.createElement('span');
            label.className = 'font-semibold text-fb-text';
            label.textContent = group.label;
            var average = document.createElement('span');
            average.className = 'text-xl font-bold text-fb-primary';
            average.textContent = Math.round(group.average) + '%';
            title.appendChild(label);
            title.appendChild(average);
            panel.appendChild(title);
            var track = document.createElement('div');
            track.className = 'mt-3 h-2 rounded-full bg-fb-bg overflow-hidden';
            var fill = document.createElement('div');
            fill.className = 'h-full rounded-full bg-fb-primary';
            fill.style.width = Math.round(group.average) + '%';
            track.appendChild(fill);
            panel.appendChild(track);
            var detail = document.createElement('p');
            detail.className = 'mt-2 text-xs text-fb-textDim';
            detail.textContent = group.count + (group.count === 1 ? ' arrangement' : ' arrangements')
                + ' · median ' + Math.round(group.median) + '%';
            panel.appendChild(detail);
            grid.appendChild(panel);
        });
        card.appendChild(grid);
        bestsCard.insertAdjacentElement('afterend', card);
    }

    // Wraps the single global entry point every mastery change already flows
    // through — the manual player slider's oninput, the Gameplay-tab speed
    // slider, and this plugin's own auto-adjust all call window.setMastery()
    // (see feedBack's player-controls.js _applyMastery). Wrapping it here,
    // rather than listening for a dedicated "mastery changed" event, catches
    // every source without needing one. Idempotent — checks a marker so a
    // plugin-runtime-idempotent.v1 re-run never double-wraps.
    function ensureMasterySaveHook() {
        if (typeof window.setMastery !== 'function' || window.setMastery.__ddWrapped) return;
        var orig = window.setMastery;
        function wrapped() {
            // apply()/arguments/return-value forwarding: this is a general-
            // purpose wrap of a shared core entry point, not a call site we
            // control, so preserve `this`, every argument, and whatever orig
            // hands back rather than assuming its current single-arg shape.
            var result = orig.apply(this, arguments);
            _onMasteryApplied(arguments[0]);
            return result;
        }
        wrapped.__ddWrapped = true;
        window.setMastery = wrapped;
    }

    // The warm-up start handed to each progress record the first time this
    // page session resumes it, with the remembered value it was derived from
    // (see _maybeRestoreSongMastery). Keyed by the record's full persistence
    // key, so no song or player inherits another's start — the state a second
    // song must not inherit is exactly what this map must keep apart. One
    // entry per record the player actually resumes, for the life of the page:
    // a session touches a handful, and reloading the page ends the session.
    var _resumeStarts = new Map();

    // True while a resume's live start is being applied, so that start does
    // not become the remembered difficulty. Persisting it would replace a
    // peak the player earned with a value they only saw for the length of a
    // warm-up, and — because the ramp can also sit inside the dead band and
    // never move — would walk the remembered value down by one offset per
    // session that ended before the first adjustment. The ramp's first move
    // (or a manual slider move) re-persists from wherever it leaves the slider.
    // Module state guarding a process-global hook: this holds while a single
    // plugin instance is live, which is the normal runtime. A plugin-runtime
    // re-run keeps the already-wrapped window.setMastery (see
    // ensureMasterySaveHook) but gets fresh module state, so the guard would
    // not reach the wrapper that does the writing.
    var _applyingResumeStart = false;

    // A write that is not a warm-up start is the player or the ramp speaking
    // for the record, so from here on the stored value is the live truth: put
    // the record's entry back to an exact baseline. Every write does this, not
    // only one following a resume — a record whose restore never ran (nothing
    // stored yet, phrase data not loaded at song:ready, no reachable setter) has
    // been live in this session all along, and must not be handed a first
    // concession on the next re-restore. Without this a manual move back onto
    // the remembered peak — which re-writes the value the start was derived
    // from, and so is indistinguishable from "nothing has happened yet" if only
    // the store is consulted — would also be undone by that re-restore.
    function _baselineResumeStart(context, value) {
        _resumeStarts.set(persistenceContextKey(context), { source: value, start: value });
    }

    function _onMasteryApplied(v, explicitContext) {
        var context = normalizePlayerContext(explicitContext || _mainPlayerContext);
        if (!_songKey || !context) return; // song/profile still loading
        var hw = window.highway;
        if (!hw || typeof hw.hasPhraseData !== 'function' || !hw.hasPhraseData()) return;
        var pct = parseInt(v, 10);
        if (!isFinite(pct)) return;
        pct = Math.max(0, Math.min(100, pct));
        // Emit updated section difficulties when mastery changes. Debounced
        // (see scheduleSectionDifficultiesEmit) rather than called directly:
        // slider drags fire oninput per pixel — window.setMastery() (and thus
        // this hook) can run many times a second, and
        // calculateAndEmitSectionDifficulties() is an O(sections*phrases)
        // recompute plus an fb.emit() that runs every listener (e.g. Section
        // Map's own re-render) synchronously in the same call stack. Running
        // that on every pixel of a drag is exactly the kind of high-frequency
        // handler CLAUDE.md's performance section calls out for debouncing.
        scheduleSectionDifficultiesEmit(context, hw);
        // Slider drags fire oninput per pixel — window.setMastery() (and thus
        // this hook) can run many times a second. Skip the parse/stringify/
        // write when the stored value hasn't actually changed.
        // A resume's warm-up start is applied through this same hook on the
        // compatibility path, and is the one value that must not overwrite the
        // remembered difficulty (see _applyingResumeStart) — the ramp's first
        // move re-persists from wherever it leaves the slider.
        if (!_applyingResumeStart) {
            writeProgress(context, { currentDifficulty: pct });
            _baselineResumeStart(context, pct);
        }
        _emitPlayerDifficultyChanged(context, pct, 'applied');
    }

    // Called once per song change (see onSongEvent). Applies this song's own
    // remembered difficulty, if any, over whatever global value core just
    // carried over from the previous song.
    //
    // Issue #112 (roadmap C4): a player's first sections of a session dip below
    // the level they settled on last time, so resuming straight at the
    // remembered peak makes early misses likely — and a rough patch this early
    // can trigger a step-down nobody needed. The first resume of a record in a
    // session therefore starts a partial ramp step lower (see
    // _resumeStartPct) and lets the existing ramp climb back; WARMUP_PHRASES
    // still holds the ramp off until there is real signal, so the concession
    // composes with the warm-up window rather than replacing it.
    //
    // Which resume is a cold start is per record, so switching to another song
    // mid-session still gets its own warm-up start. A *re*-restore of the
    // record already in play — song:ready re-firing on a reconnect, a
    // player-context relink, a split panel re-registering under a changed
    // identity — must instead put the player back where the start left them:
    // the start is deliberately not re-recorded (see _applyingResumeStart), so
    // re-deriving it from the store here would hand back the peak and cancel
    // the concession. It is re-derived only once something has re-recorded the
    // difficulty since — the ramp's first adjustment, or a manual slider move
    // (see _baselineResumeStart).
    function _maybeRestoreSongMastery(context, explicitHighway) {
        var ctx = normalizePlayerContext(context);
        if (!ctx) return false;
        var hw = explicitHighway || ctx.highway
            || ((ctx.compatibility_adapter || ctx.player_id === 'main') ? window.highway : null);
        if (!hw || typeof hw.hasPhraseData !== 'function' || !hw.hasPhraseData()) return false;
        var record = readProgress(ctx);
        var saved = record && _pct(record.currentDifficulty);
        if (saved === null) return false;
        var recordKey = persistenceContextKey(ctx);
        var previous = _resumeStarts.get(recordKey);
        var start;
        if (!previous) {
            start = _resumeStartPct(saved); // cold start: not resumed in this page session yet
        } else if (saved === previous.source) {
            // Nothing has re-recorded the difficulty since. Re-floored because
            // the entry holds the number handed out at the time, and Min % can
            // have been raised since. Auto-adjust can also have been turned off
            // since — in Settings, or by the originless-drift stand-down — and
            // that removes the ramp this concession was meant to be climbed
            // back by, so hand back the remembered value instead, exactly as
            // the cold start above declines to offset in Standard mode.
            start = settings.autoAdjust ? Math.max(settings.minMastery, previous.start) : saved;
        } else {
            start = saved; // the ramp (or a manual move) has moved on; the store is the truth
        }
        var wasApplying = _applyingResumeStart;
        _applyingResumeStart = true;
        var applied;
        try {
            applied = _applyDifficultyForContext(ctx, start, hw, 'restore');
        } finally {
            // Restored, not cleared: applying a start runs host code
            // synchronously (a capability dispatch handler, or window.setMastery
            // fanning out through the difficulty:player-changed event), and that
            // code can land here again. Clearing instead would let the outer
            // apply persist its start and walk the remembered value down.
            _applyingResumeStart = wasApplying;
        }
        // Recorded only once the player actually has it, so a restore that
        // reached nothing spends nothing — the next attempt is a first start,
        // and derives the same value from the unchanged store regardless.
        if (applied) _resumeStarts.set(recordKey, { source: saved, start: start });
        return applied;
    }

    // How far below a cold start begins, as a fraction of the ramp step
    // `thresholds().step` already moves the slider in a single full step
    // (RESUME_OFFSET_FRACTION, above). Applies only with auto-adjust on: the
    // ramp is what climbs the gap, and in Standard mode nothing would, so the
    // offset would strand the player below their own remembered difficulty
    // until they moved the slider by hand.
    function _resumeStartPct(savedPct) {
        if (!settings.autoAdjust) return savedPct;
        var offset = Math.round(thresholds().step * RESUME_OFFSET_FRACTION);
        if (!isFinite(offset) || offset <= 0) return savedPct;
        // No room below the floor to give, which is the normal state of a song
        // the player has already bottomed out on — and also the case where the
        // floor sits above `saved` (the player raised Min %, or hand-set the
        // slider below it). Offsetting then would be a step *up*, the opposite
        // of the concession, so decline instead.
        if (savedPct <= settings.minMastery) return savedPct;
        return Math.max(settings.minMastery, savedPct - offset);
    }

    function _restoreOrScheduleSections(context, highway) {
        if (!_maybeRestoreSongMastery(context, highway)) {
            scheduleSectionDifficultiesEmit(context, highway);
        }
    }

    // ---- Settings (localStorage-backed; see settings.html for the panel) ----
    var settings = {
        autoAdjust: lsGet('autoAdjust', false),
        dropResistance: lsGet('dropResistance', false) === true,
        levelUpOnly: lsGet('levelUpOnly', false) === true,
        showDifficultyGuide: _resolveDifficultyGuideSetting(),
        sensitivity: lsGet('sensitivity', 2),     // 1 (lenient) .. 3 (strict) — confidence thresholds + step size
        downStepRatio: lsGet('downStepRatio', 1), // 1..2 — downward target multiplier; upward target is unchanged
        reactionSpeed: lsGet('reactionSpeed', 2), // 1 (slow) .. 3 (fast) — EMA_ALPHA, how much one phrase's result moves the rolling average
        minMastery: lsGet('minMastery', 0),     // percent
        maxMastery: lsGet('maxMastery', 100),   // percent
        generateLevels: lsGet('generateLevels', 4), // 2..8 cap — phrase-ladder tier cap sent to /generate
    };

    // Issue #64: minMastery/maxMastery are read and written independently
    // (settings.html's two number inputs each call window._ddSet on their
    // own onchange). An inverted pair (min > max — a stale write from an
    // older plugin version, a manual localStorage edit, a race between two
    // open tabs) breaks the "auto-adjust will never cross these bounds"
    // invariant the README promises: the clamp below is
    // Math.max(min, Math.min(max, next)), which returns min when min > max,
    // i.e. a value above the configured maximum. Swapping restores a valid
    // interval regardless of which field is "wrong" without discarding
    // either configured number. Called on initial load and whenever either
    // bound changes via the storage/settings-changed listeners below.
    function _normalizeMasteryBounds() {
        var min = settings.minMastery, max = settings.maxMastery;
        if (typeof min !== 'number' || !isFinite(min)) min = 0;
        if (typeof max !== 'number' || !isFinite(max)) max = 100;
        min = Math.max(0, Math.min(100, min));
        max = Math.max(0, Math.min(100, max));
        if (min > max) { var tmp = min; min = max; max = tmp; }
        settings.minMastery = min;
        settings.maxMastery = max;
    }
    _normalizeMasteryBounds();

    function thresholds() {
        var s = Math.max(1, Math.min(3, settings.sensitivity));
        return {
            up: 0.93 - (s - 1) * 0.05,     // 1:0.93  2:0.88  3:0.83
            down: 0.65 + (s - 1) * 0.03,   // 1:0.65  2:0.68  3:0.71
            step: 10 + (s - 1) * 5,        // percent step: 10 / 15 / 20
        };
    }

    // reactionSpeed's default (2) resolves to 0.35 — the value EMA_ALPHA was
    // hardcoded to before this setting existed — so a user who never touches
    // the new slider sees byte-identical auto-adjust behavior to before
    // (issue #5's acceptance criterion). 1 (slow) smooths more, weighting a
    // single phrase's result less; 3 (fast) reacts to a run of good/bad
    // sections sooner.
    function emaAlpha() {
        var s = Math.max(1, Math.min(3, settings.reactionSpeed));
        return 0.20 + (s - 1) * 0.15; // 1:0.20  2:0.35  3:0.50
    }

    // Fixed for now (not settings) — kept as named top-level constants, same
    // treatment as thresholds()/emaAlpha() above, so a future settings-slider
    // addition can follow the exact pattern already established for
    // sensitivity/reactionSpeed.
    const WARMUP_PHRASES = 2;  // phrases scored on a fresh song before auto-adjust may act
    const RAMP_PHRASES = 3;    // qualifying phrases a full th.step move is spread over
    // Issue #112: how far below the remembered difficulty a cold start lands,
    // as a fraction of the ramp step `thresholds().step` already moves the
    // slider in one full step (10 / 15 / 20 by Sensitivity — the same step
    // auto-adjust takes upward, before any Difficulty drop speed multiplier).
    // Must stay at or below 1: a larger offset would drop the player further
    // in one call than auto-adjust itself moves them, which reads as losing
    // their place rather than easing them back in. At 0.5 the existing ramp
    // closes the gap once it has the evidence to move. See _resumeStartPct
    // for the auto-adjust gate and _maybeRestoreSongMastery for the
    // once-per-record scope.
    const RESUME_OFFSET_FRACTION = 0.5;
    const DOWN_CONFIRM_PHRASES = 2;
    const MASTERY_STREAK_PHRASES = 3;
    const MASTERY_STREAK_ACCURACY = 0.95;

    // ---- Player-context compatibility and registry ----------------------
    var _playerContexts = new Map();
    var _mainPlayerContext = null;
    var _mainContextResolution = 0;

    function _profileApisPresent() {
        var fb = window.feedBack;
        return !!((window.v3Profile && typeof window.v3Profile.get === 'function')
            || (fb && fb.playerContexts && typeof fb.playerContexts.getActive === 'function'));
    }

    function _songContextFields(si) {
        si = _plainObject(si) || {};
        var currentSong = _plainObject(window.feedBack && window.feedBack.currentSong) || {};
        // arrangement_type is the field name the Host's getSongInfo() (and
        // _instrumentKind()'s caller in onSongEvent) actually uses for the
        // arrangement classifier — si.type is the WebSocket message
        // discriminator, not the instrument/arrangement kind. Without this,
        // a currentSong that doesn't duplicate the classifier under
        // instrument/instrument_id/type falls through to 'legacy-unknown'.
        var rawType = _id(si.instrument_id ?? si.instrument ?? si.arrangement_type ?? si.type
            ?? currentSong.instrument_id ?? currentSong.instrument ?? currentSong.arrangement_type ?? currentSong.type, '');
        var rawRole = _id(si.role ?? si.role_id ?? currentSong.role ?? currentSong.role_id, '');
        var arrangementName = _id(si.arrangement ?? si.arrangement_name
            ?? currentSong.arrangement ?? currentSong.arrangementName, '');
        if (!rawRole && _isKaraokeRole(rawType || arrangementName)) rawRole = 'karaoke';
        if (!rawRole) rawRole = /^(lead|rhythm|bass|combo|chord|humstrum)$/i.test(rawType)
            ? rawType.toLowerCase() : 'instrumental';
        var instrument = rawType;
        if (_isKaraokeRole(rawRole) || _isKaraokeRole(rawType)) instrument = 'voice';
        else if (/^(piano|keys|keyboard|synth)$/i.test(rawType)) instrument = 'keys';
        else if (/^(lead|rhythm|combo|chord|humstrum)$/i.test(rawType)) instrument = 'guitar';
        if (!instrument) instrument = 'legacy-unknown';
        var arrangementId = si.arrangement_index;
        if (arrangementId == null) arrangementId = currentSong.arrangementIndex;
        if (arrangementId == null) arrangementId = arrangementName || '0';
        return {
            song_id: currentSong.filename || si.filename || 'unknown-song',
            arrangement_id: arrangementId,
            instrument: instrument,
            role: rawRole,
            skill: 'overall',
        };
    }

    function _compatibilityContext(profile, si) {
        profile = _plainObject(profile);
        if (!profile) return null;
        return normalizePlayerContext(Object.assign({}, _songContextFields(si), {
            session_id: _sessionId,
            player_id: 'main',
            profile_id: profile.profile_id ?? profile.profileId ?? profile.id,
            profile_hash: profile.profile_hash ?? profile.profileHash ?? profile.player_hash ?? profile.hash,
            profile_ready: profile.profile_ready !== false && profile.ready !== false,
            highway: window.highway || null,
            compatibility_adapter: true,
        }));
    }

    // Returns either a ready context or a Promise for one. Crucially, the
    // presence of a profile API suppresses the legacy-default fallback even
    // while get()/getActive() is pending or returns no profile.
    function resolveCompatibilityPlayerContext(si) {
        var fb = window.feedBack;
        var value;
        if (fb && fb.playerContexts && typeof fb.playerContexts.getActive === 'function') {
            value = fb.playerContexts.getActive('main');
        } else if (window.v3Profile && typeof window.v3Profile.get === 'function') {
            value = window.v3Profile.get();
        } else {
            return _compatibilityContext({ id: 'legacy-default' }, si);
        }
        if (value && typeof value.then === 'function') {
            return value.then(function (profile) { return _compatibilityContext(profile, si); });
        }
        return _compatibilityContext(value, si);
    }

    function _reportCompatibilityProfileError(error, resolutionId) {
        if (resolutionId !== _mainContextResolution) return null;
        var message = error && error.message ? String(error.message) : String(error || 'unknown profile error');
        if (window.console && typeof window.console.warn === 'function') {
            window.console.warn('[difficulty_ladder] main profile context resolution failed:', error);
        }
        var fb = window.feedBack;
        if (fb && typeof fb.emit === 'function') fb.emit('difficulty:profile-context-error', {
            schema: 'difficulty_ladder.profile_context_error.v1',
            player_id: 'main',
            message: message,
        });
        // Keep persistence and unscoped Section Map output gated. A later
        // song/profile lifecycle activation gets a fresh resolutionId and can recover.
        return null;
    }

    function _acceptMainPlayerContext(context, resolutionId, previousIdentitySignature) {
        if (resolutionId !== _mainContextResolution) return null;
        var ctx = normalizePlayerContext(context);
        if (!ctx) return null;
        _mainPlayerContext = ctx;
        _songInstrument = ctx.instrument;
        // The song/arrangement can stay the same across a compatibility
        // profile switch (song:ready re-fires on a reconnect/restart without
        // _songKey changing), so onSongEvent()'s key-change check alone
        // won't reset the scorer. Compare the resolved identity's own
        // persistence key against the one in effect before this resolution
        // started and reset here whenever it differs, so a new profile never
        // inherits the outgoing profile's EMA/warm-up/judgment state.
        if (persistenceContextKey(ctx) !== previousIdentitySignature) resetPerSongState();
        migrateLegacyData(ctx);
        _restoreOrScheduleSections(ctx, window.highway);
        return ctx;
    }

    function activateCompatibilityPlayerContext(si) {
        var resolutionId = ++_mainContextResolution;
        var previousIdentitySignature = persistenceContextKey(_mainPlayerContext);
        _mainPlayerContext = null; // gate writes while a new identity resolves
        var resolved;
        try {
            resolved = resolveCompatibilityPlayerContext(si);
        } catch (error) {
            return _reportCompatibilityProfileError(error, resolutionId);
        }
        if (resolved && typeof resolved.then === 'function') {
            return resolved.then(
                function (context) { return _acceptMainPlayerContext(context, resolutionId, previousIdentitySignature); },
                function (error) { return _reportCompatibilityProfileError(error, resolutionId); }
            );
        }
        return _acceptMainPlayerContext(resolved, resolutionId, previousIdentitySignature);
    }

    function upsertPlayerContext(raw) {
        raw = _plainObject(raw && raw.context) || raw;
        var context = normalizePlayerContext(raw);
        if (!context) return null;
        var key = playerContextKey(context);
        var previous = _playerContexts.get(key);
        if (!context.highway && previous && previous.highway) context.highway = previous.highway;
        var highwayChanged = !!(previous && previous.highway && context.highway
            && previous.highway !== context.highway);
        var identityChanged = !!previous && (
            persistenceContextKey(previous) !== persistenceContextKey(context) || highwayChanged
        );
        if (identityChanged) _cancelSectionDifficultiesForPlayer(key);
        _playerContexts.set(key, context);

        // Detector construction and profile selection may arrive in either
        // order. Relink by stable session/player identity, reset every scorer
        // cursor/EMA before changing persistence identity, then restore only
        // the new context's saved difficulty.
        _splitScoreStates.forEach(function (state, hw) {
            if (state.playerKey !== key) return;
            if (context.highway && hw !== context.highway) {
                _splitScoreStates.delete(hw);
                return;
            }
            if (identityChanged || persistenceContextKey(state.context) !== persistenceContextKey(context)) {
                _resetSplitScoreState(state, context, key);
                _restoreOrScheduleSections(context, hw);
            } else {
                state.context = context;
                scheduleSectionDifficultiesEmit(context, hw);
            }
        });
        // The main player is scored by tickScoring()'s own window.highway
        // path (and persisted through _mainPlayerContext), not through the
        // split-scorer map. Registering it here too — a Host's
        // player-context payload for "main" can legitimately carry a
        // highway reference — would score every note twice per frame
        // (tickSplitScoring() AND the main path) and let the two scorers
        // fight over which one's mastery change was a "manual override".
        if (context.highway && context.player_id !== 'main') registerSplitHighway(context.highway, context);

        if (context.player_id === 'main') {
            var mainChanged = persistenceContextKey(_mainPlayerContext) !== persistenceContextKey(context);
            ++_mainContextResolution; // a pending global-profile lookup must not overwrite this event
            _mainPlayerContext = context;
            _songKey = context.song_id + '::' + context.arrangement_id;
            _songInstrument = context.instrument;
            if (mainChanged) resetPerSongState();
            _restoreOrScheduleSections(context, context.highway || window.highway);
        }
        return context;
    }

    function removePlayerContext(raw) {
        raw = _plainObject(raw && raw.context) || raw;
        raw = _plainObject(raw) || {};
        // left payloads need only identify the session/player pair. Match the
        // same implicit session used by normalizePlayerContext during upsert.
        var key = playerContextKey({
            session_id: _id(raw.session_id ?? raw.sessionId, _sessionId),
            player_id: raw.player_id ?? raw.playerId,
        });
        if (!key) return false;
        _splitScoreStates.forEach(function (state, hw) {
            if (state.playerKey === key) _splitScoreStates.delete(hw);
        });
        _cancelSectionDifficultiesForPlayer(key);
        var removed = _playerContexts.delete(key);
        if (_mainPlayerContext && playerContextKey(_mainPlayerContext) === key) {
            ++_mainContextResolution;
            _mainPlayerContext = null;
            resetPerSongState();
        }
        return removed;
    }

    function listPlayerContexts() {
        return Array.from(_playerContexts.values());
    }

    function _contextEventPayload(context) {
        var ctx = normalizePlayerContext(context);
        if (!ctx) return null;
        return {
            schema: PLAYER_CONTEXT_SCHEMA,
            session_id: ctx.session_id, player_id: ctx.player_id,
            profile_id: ctx.profile_id, profile_hash: ctx.profile_hash,
            song_id: ctx.song_id, arrangement_id: ctx.arrangement_id,
            instrument: ctx.instrument, role: ctx.role, skill: ctx.skill,
        };
    }

    function _emitPlayerDifficultyChanged(context, pct, reason) {
        var fb = window.feedBack;
        var playerContext = _contextEventPayload(context);
        if (!playerContext || !fb || typeof fb.emit !== 'function') return;
        fb.emit('difficulty:player-changed', {
            schema: 'difficulty_ladder.difficulty_event.v1',
            player_context: playerContext,
            current_difficulty: _pct(pct),
            reason: reason || 'unknown',
        });
    }

    // Shared tail of both apply paths below. The persistence write is skipped
    // for a resume's warm-up start (see _applyingResumeStart) so the start the
    // player is given is not mistaken for a difficulty they settled on.
    function _finishDifficultyApply(ctx, value, hw, reason) {
        if (!_applyingResumeStart) {
            writeProgress(ctx, { currentDifficulty: value });
            _baselineResumeStart(ctx, value);
        }
        _emitPlayerDifficultyChanged(ctx, value, reason);
        scheduleSectionDifficultiesEmit(ctx, hw);
    }

    function _applyDifficultyForContext(context, pct, explicitHighway, reason) {
        var ctx = normalizePlayerContext(context);
        var value = _pct(pct);
        if (!ctx || value === null) return false;
        var hw = explicitHighway || ctx.highway;
        var fb = window.feedBack;
        if (!ctx.compatibility_adapter && fb && fb.capabilities
            && typeof fb.capabilities.dispatch === 'function') {
            try {
                var dispatched = fb.capabilities.dispatch('player-difficulty.v1', {
                    schema: 'difficulty_ladder.difficulty_request.v1',
                    action: 'set', player_context: _contextEventPayload(ctx),
                    current_difficulty: value, reason: reason || 'adaptive',
                });
                if (dispatched === true) {
                    _finishDifficultyApply(ctx, value, hw, reason);
                    return true;
                }
            } catch (_) { /* fall through to the context-owned highway */ }
        }
        if (hw && typeof hw.setMastery === 'function' && !(ctx.compatibility_adapter && hw === window.highway)) {
            hw.setMastery(value / 100);
            _finishDifficultyApply(ctx, value, hw, reason);
            return true;
        }
        if (ctx.compatibility_adapter && typeof window.setMastery === 'function') {
            window.setMastery(value); // wrapped hook persists and emits
            return true;
        }
        return false;
    }

    // ---- Per-song scoring state ----
    // `_mainScore` is the main player's score state -- the DEFAULT STATE of
    // the shared machine below (same fields a per-panel split state holds).
    // The old per-field `let` bindings are gone: `_mainScore` IS the
    // storage, so there is no sync layer and no way for two copies to
    // disagree. Readers that used the `let`s read `_mainScore.*` now.
    // (Declared here so `resetPerSongState` can use it; the machine section
    // documents the field-parity contract with the split initializer. The
    // declaration assignment below runs at load, BEFORE `resetPerSongState()`
    // is first called -- `_resetMainScoreState` therefore only ever refreshes
    // fields and never allocates the object, so a state reference handed out
    // earlier (a split-style caller holding `_mainScore`) stays valid.)
    var _mainScore = {
        judgedKeys: new Set(),
        pendingJudgments: new Map(),
        phraseHits: 0,
        phraseTotal: 0,
        phraseJudgments: [],
        phrasesScored: 0,
        curPhraseIdx: -1,
        lastScoredT: -1,
        lastScoredWallT: -1,
        noteCursor: 0,
        chordCursor: 0,
        emaHitRate: null,
        lastObservedMasteryPct: null,
        lastAutoAction: null,
        rampDirection: null,
        rampProgress: 0,
        downStreak: 0,
        manualOverride: false,
        context: null,
        playerKey: null,
    };
    let _songKey = null;
    let _songInstrument = null;    // authoritative routes.py classification when available
    // EMA weight is now the reactionSpeed setting (emaAlpha(), above) rather
    // than a hardcoded constant — see issue #5. Read live (not cached) since
    // the settings-changed listener below can update settings.reactionSpeed
    // mid-song.
    let _masteryStreak = 0;     // consecutive high-accuracy phrases at configured max mastery
    // Forward-advancing cursors into the time-sorted notes/chords arrays —
    // avoids an O(N) full-array rescan every rAF tick (CLAUDE.md's per-frame
    // performance doctrine). Reset only on a backward seek (loop/rewind).
    let _hudMaxDifficulty = null;
    let _hudBadgeMeasureKey = null;
    let _hudBadgeWidth = 0;

    function downStepRatio() {
        var ratio = Number(settings.downStepRatio);
        return isFinite(ratio) ? Math.max(1, Math.min(2, ratio)) : 1;
    }

    function rampStep(th, progress, direction) {
        const index = Math.max(0, Math.min(RAMP_PHRASES - 1, Number(progress) || 0));
        // Keep the ramp curve symmetric; only its final target differs by
        // direction. Rounding the target once guarantees the three increments
        // still total an integer mastery percentage.
        const target = Math.round(th.step * (direction === 'down' ? downStepRatio() : 1));
        const before = Math.round(target * index / RAMP_PHRASES);
        const after = Math.round(target * (index + 1) / RAMP_PHRASES);
        return Math.max(1, after - before);
    }

    // Which way this phrase's rolling accuracy says to move the slider — the
    // one seam both the main and the split-highway ramp read, so a change here
    // covers both. `levelUpOnly` (#111) is a comfort switch, not a learning
    // one: a below-threshold phrase yields no direction at all rather than a
    // 'down', which keeps the cold-start/warm-up accounting and the manual
    // override detection untouched. Turning it back off resumes normal
    // step-downs from the current position on the next qualifying phrase.
    function rampDirection(emaHitRate, th) {
        if (emaHitRate >= th.up) return 'up';
        if (emaHitRate <= th.down && !settings.levelUpOnly) return 'down';
        return null;
    }

    function songKeyOf(si) {
        if (!si) return null;
        const arrKey = (si.arrangement_index != null) ? si.arrangement_index : (si.arrangement || '');
        return (si.filename || '') + '::' + arrKey;
    }

    function resetPerSongState() {
        _resetMainScoreState();
        _masteryStreak = 0;
        _hudMaxDifficulty = null;
        _hudBadgeMeasureKey = null;
        _hudBadgeWidth = 0;
        _hudPhraseIdx = -1;
    }
    resetPerSongState();

    function judgmentKey(time, s, f) { return time + '_' + s + '_' + f; }

    function _phraseIdOf(songKey, idx, phrase) {
        if (!songKey || idx == null || idx < 0 || !phrase) return null;
        return [
            songKey,
            idx,
            Number(phrase.start_time || 0).toFixed(3),
            Number(phrase.end_time || 0).toFixed(3),
        ].join('::');
    }

    // Issue #63: the discrete difficulty-tier fill math, factored out so
    // every glass renderer -- this plugin's own player HUD (drawHud) and the
    // per-section aggregate it emits for feedBack-plugin-sectionmap
    // (calculateAndEmitSectionDifficulties) -- presents the same tier for
    // the same (mastery, max_difficulty) pair. Before this fix the two used
    // different formulas (a discrete tier here vs. a continuous
    // mastery-scaled fraction in the emitted event) that could disagree
    // materially for a lower-depth phrase/section.
    // `maxDifficulty <= 0` means there's no tier ladder to climb -- nothing
    // left to fill toward, so it's reported as fully filled (matches the
    // pre-existing "no glass to fill toward" convention both call sites
    // already followed for this case).
    //
    // `topDifficulty` (optional, defaults to maxDifficulty) is the tier from
    // which the phrase plays in full -- core's getPhrases().top_difficulty.
    // Generated ladders share one arrangement-wide tier scale, so an easy
    // phrase is complete well below max_difficulty; its glass is full from
    // that tier on rather than only at the very top of the slider.
    function _tierFillFrac(mastery, maxDifficulty, topDifficulty) {
        if (!isFinite(maxDifficulty) || maxDifficulty <= 0) return { idxLevel: 0, fillFrac: 1 };
        var top = isFinite(topDifficulty) ? Math.min(topDifficulty, maxDifficulty) : maxDifficulty;
        var clamped = Math.max(0, Math.min(1, mastery));
        var idxLevel = Math.min(maxDifficulty, Math.floor(clamped * (maxDifficulty + 1)));
        return { idxLevel: idxLevel, fillFrac: top <= 0 ? 1 : Math.min(1, idxLevel / top) };
    }

    // True when the full (unfiltered) chart has a note or chord onset in
    // [t0, t1). Only consulted for sections whose phrases are all single-level,
    // from the throttled section emit -- never a per-frame path.
    function _chartHasContentIn(hw, t0, t1) {
        var lists = [
            typeof hw.getNotes === 'function' ? hw.getNotes() : null,
            typeof hw.getChords === 'function' ? hw.getChords() : null,
        ];
        return lists.some(function (list) {
            return Array.isArray(list) && list.some(function (ev) {
                var t = Number(ev && ev.t);
                return t >= t0 && t < t1;
            });
        });
    }

    // How hard a phrase is, for glass sizing: the tier where it becomes
    // complete. Falls back to max_difficulty on a core that predates
    // getPhrases().top_difficulty (identical for fully authored ladders).
    function _phraseTopDifficulty(phrase) {
        var top = Number(phrase && phrase.top_difficulty);
        return isFinite(top) ? top : Number(phrase && phrase.max_difficulty) || 0;
    }

    function _presentedDifficultyLevel(hw, phrase) {
        const max = Number(phrase?.max_difficulty);
        let mastery;
        const checks = [
            { check: () => !hw || !phrase || typeof hw.getMastery !== 'function', result: () => null },
            { check: () => !isFinite(max) || max <= 0, result: () => 0 },
            { check: () => { mastery = Number(hw.getMastery()); return !isFinite(mastery); }, result: () => null },
            { check: () => true, result: () => _tierFillFrac(mastery, max).idxLevel }
        ];
        const { result } = checks.find(c => c.check());
        return result();
    }

    function loadPhraseAttemptStore() { return _phraseAttemptStore.load(); }

    function _defaultPersistenceContext() {
        if (_mainPlayerContext) return _mainPlayerContext;
        if (_profileApisPresent()) return null;
        return normalizePlayerContext({
            session_id: _sessionId, player_id: 'main', profile_id: 'legacy-default',
            song_id: 'unknown-song', arrangement_id: '0', instrument: 'legacy-unknown',
            role: 'instrumental', skill: 'overall', compatibility_adapter: true,
        });
    }

    // See the matching eslint-disable block around _profilePlayerNode/
    // _progressSkillNode/readProgress above: every bracket key here is
    // _nodeKey()/_profileKey()-derived and prototype-pollution-safe.
    /* eslint-disable security/detect-object-injection */
    function _phraseAttemptNode(store, context, create) {
        var ctx = normalizePlayerContext(context || _defaultPersistenceContext());
        if (!ctx) return null;
        var profileKey = _profileKey(ctx), profile = store.profiles[profileKey];
        if (!profile && create) profile = store.profiles[profileKey] = {
            profile_id: ctx.profile_id, profile_hash: ctx.profile_hash, players: {},
        };
        var player = _profilePlayerNode(profile, ctx, create);
        if (!player) return null;
        var songKey = _nodeKey(ctx.song_id), songNode = player.songs[songKey];
        if (!songNode && create) songNode = player.songs[songKey] = { song_id: ctx.song_id, arrangements: {} };
        if (!_plainObject(songNode) || (!_plainObject(songNode.arrangements) && !create)) return null;
        if (!_plainObject(songNode.arrangements)) songNode.arrangements = {};
        var arrangementKey = _nodeKey(ctx.arrangement_id), arrangementNode = songNode.arrangements[arrangementKey];
        if (!arrangementNode && create) arrangementNode = songNode.arrangements[arrangementKey] = {
            arrangement_id: ctx.arrangement_id, instruments: {},
        };
        if (!_plainObject(arrangementNode) || (!_plainObject(arrangementNode.instruments) && !create)) return null;
        if (!_plainObject(arrangementNode.instruments)) arrangementNode.instruments = {};
        var instrumentKey = _nodeKey(ctx.instrument), instrumentNode = arrangementNode.instruments[instrumentKey];
        if (!instrumentNode && create) instrumentNode = arrangementNode.instruments[instrumentKey] = {
            instrument: ctx.instrument, roles: {},
        };
        if (!_plainObject(instrumentNode) || (!_plainObject(instrumentNode.roles) && !create)) return null;
        if (!_plainObject(instrumentNode.roles)) instrumentNode.roles = {};
        var roleKey = _nodeKey(ctx.role), roleNode = instrumentNode.roles[roleKey];
        if (!roleNode && create) roleNode = instrumentNode.roles[roleKey] = { role: ctx.role, skills: {} };
        if (!_plainObject(roleNode) || (!_plainObject(roleNode.skills) && !create)) return null;
        if (!_plainObject(roleNode.skills)) roleNode.skills = {};
        var skillKey = _nodeKey(ctx.skill), node = roleNode.skills[skillKey];
        if (!node && create) node = roleNode.skills[skillKey] = { skill: ctx.skill, attempts: [] };
        if (!_plainObject(node) || (!Array.isArray(node.attempts) && !create)) return null;
        if (!Array.isArray(node.attempts)) node.attempts = [];
        return node;
    }

    function _legacyUnscopedPhraseAttempts(store, context) {
        var ctx = normalizePlayerContext(context);
        if (!ctx || ctx.skill !== 'overall') return [];
        var profile = store.profiles[_profileKey(ctx)];
        var player = _profilePlayerNode(profile, ctx, false);
        var songNode = player && player.songs[_nodeKey(ctx.song_id)];
        var arrangementNode = songNode && songNode.arrangements
            && songNode.arrangements[_nodeKey(ctx.arrangement_id)];
        var instruments = arrangementNode && arrangementNode.instruments;
        var matches = [];
        if (!_plainObject(instruments)) return matches;
        Object.keys(instruments).forEach(function (instrumentKey) {
            var instrumentNode = instruments[instrumentKey];
            // Same instrument scoping as readProgress's fallback scan: only
            // a record whose source instrument was itself unknown at
            // migration time may seed an arbitrary instrument's context.
            var instrumentMatches = instrumentKey === _nodeKey(ctx.instrument)
                || (instrumentNode && instrumentNode.instrument === 'legacy-unknown');
            if (!instrumentMatches) return;
            var roles = instrumentNode && instrumentNode.roles;
            if (!_plainObject(roles)) return;
            Object.keys(roles).forEach(function (roleKey) {
                var skills = roles[roleKey] && roles[roleKey].skills;
                var overall = skills && skills[_nodeKey('overall')];
                if (!overall || !Array.isArray(overall.attempts)) return;
                overall.attempts.forEach(function (attempt) {
                    if (attempt && attempt.legacy_unscoped_instrument === true
                        && attempt.legacy_claim_player_id === ctx.player_id) matches.push(attempt);
                });
            });
        });
        return matches;
    }
    /* eslint-enable security/detect-object-injection */

    function loadPhraseAttempts(context) {
        var ctx = normalizePlayerContext(context || _defaultPersistenceContext());
        if (!ctx) return [];
        var store = loadPhraseAttemptStore();
        var node = _phraseAttemptNode(store, ctx, false);
        var exact = node ? node.attempts : [];
        var seen = new Set(exact.map(function (attempt) { return attempt && attempt.legacy_id; }).filter(Boolean));
        var fallback = _legacyUnscopedPhraseAttempts(store, ctx).filter(function (attempt) {
            if (!attempt.legacy_id || seen.has(attempt.legacy_id)) return false;
            seen.add(attempt.legacy_id);
            return true;
        });
        return exact.concat(fallback).slice(-MAX_PHRASE_ATTEMPTS);
    }

    function savePhraseAttempts(attempts, context) {
        var store = loadPhraseAttemptStore();
        var node = _phraseAttemptNode(store, context, true);
        if (!node) return false; // profile API exists but identity is not ready
        node.attempts = Array.isArray(attempts) ? attempts.slice(-MAX_PHRASE_ATTEMPTS) : [];
        return _phraseAttemptStore.write();
    }

    function flushPhraseAttempts() { _phraseAttemptStore.flush(); }

    function recordPhraseAttempt(ratio, explicitContext, scoreState, explicitHighway) {
        var context = normalizePlayerContext(explicitContext || _mainPlayerContext);
        // The main player passes its own `_mainScore`; split panels pass
        // their per-highway state; direct callers may pass null, which reads
        // the main player's ledger. `scoreState` must be a state object or
        // null/undefined -- a truthy non-object would read garbage fields.
        var ledger = scoreState == null ? _mainScore : scoreState;
        if (typeof ledger !== 'object') return false;
        var phraseIdx = ledger.curPhraseIdx;
        var phraseTotal = ledger.phraseTotal;
        var phraseHits = ledger.phraseHits;
        var phraseJudgments = Array.isArray(ledger.phraseJudgments)
            ? ledger.phraseJudgments : [];
        var hw = explicitHighway || (context && context.highway)
            || (!context ? window.highway : null);
        if (!context || phraseIdx < 0 || phraseTotal <= 0 || !hw) return false;
        // phraseIdx is a numeric array index (curPhraseIdx), not a property
        // name — plain array indexing, immune to the prototype-pollution
        // class detect-object-injection otherwise guards against.
        // eslint-disable-next-line security/detect-object-injection
        const phrase = hw.getPhrases?.()?.[phraseIdx];
        const scopedSongKey = context.song_id + '::' + context.arrangement_id;
        const phraseId = _phraseIdOf(scopedSongKey, phraseIdx, phrase);
        if (!phraseId) return false;
        var attemptNode = _phraseAttemptNode(loadPhraseAttemptStore(), context, true);
        if (!attemptNode) return false;
        const attempts = attemptNode.attempts;
        attempts.push({
            schema: 'difficulty_ladder.phrase_attempt.v2',
            session_id: context.session_id,
            player_id: context.player_id,
            profile_id: context.profile_id,
            profile_hash: context.profile_hash,
            song_id: context.song_id,
            arrangement_id: context.arrangement_id,
            instrument: context.instrument,
            role: context.role,
            skill: context.skill,
            phrase_id: phraseId,
            phrase_index: phraseIdx,
            phrase_start_time: phrase.start_time,
            phrase_end_time: phrase.end_time,
            presented_difficulty: _presentedDifficultyLevel(hw, phrase),
            hit: ratio >= 1,
            hit_count: phraseHits,
            miss_count: phraseTotal - phraseHits,
            note_count: phraseTotal,
            hit_rate: ratio,
            note_results: phraseJudgments.slice(),
            timestamp: new Date().toISOString(),
        });
        attemptNode.attempts = attempts.slice(-MAX_PHRASE_ATTEMPTS);
        _phraseAttemptStore.markDirty();
        return true;
    }

    function _legacySongIdentity(key) {
        var text = _id(key);
        if (!text) return null;
        var split = text.lastIndexOf('::');
        return split > 0
            ? { song_id: text.slice(0, split), arrangement_id: text.slice(split + 2) || '0' }
            : { song_id: text, arrangement_id: '0' };
    }

    function _legacyRoleForInstrument(instrument) {
        return _isKaraokeRole(instrument) || /^(voice|vocals)$/i.test(instrument) ? 'karaoke' : 'instrumental';
    }

    function _legacyInstrumentForValue(instrument) {
        return /^fretted$/i.test(_id(instrument, '')) ? 'guitar' : _id(instrument, '');
    }

    // Legacy storage has no profile identity. It is therefore claimed once,
    // only by a confirmed single-player compatibility context. Explicit
    // concurrent contexts never call this path. Source keys are deliberately
    // retained so a migration can be inspected or recovered.
    function migrateLegacyData(context) {
        var ctx = normalizePlayerContext(context);
        if (!ctx || !ctx.compatibility_adapter) return false;
        var claimedBy = _profileKey(ctx);
        var progress = loadProgressStore();
        var progressMigration = progress.migrations.songMasteryV1;
        if (!progressMigration) {
            var legacyMap = loadSongMasteryMap();
            // key here is one of Object.keys(legacyMap) — a read of an
            // already-enumerated own property, not an externally-chosen
            // key — so the bracket reads below can't be redirected.
            /* eslint-disable security/detect-object-injection */
            Object.keys(legacyMap).forEach(function (key) {
                var identity = _legacySongIdentity(key);
                var currentDifficulty = _masteryPct(legacyMap[key]);
                if (!identity || currentDifficulty === null) return;
                var rawRecord = _plainObject(legacyMap[key]);
                var sourceInstrument = _id(rawRecord && rawRecord.instrument, '');
                var instrument = _legacyInstrumentForValue(sourceInstrument) || 'legacy-unknown';
                var sourceRole = _id(rawRecord && rawRecord.role, '');
                var legacyContext = Object.assign({}, ctx, identity, {
                    instrument: instrument,
                    role: sourceRole || _legacyRoleForInstrument(instrument),
                    skill: 'overall',
                });
                var existing = _readExactProgress(progress, legacyContext);
                if (!existing || _pct(existing.currentDifficulty) === null) {
                    _writeProgressToStore(progress, legacyContext, {
                        currentDifficulty: currentDifficulty,
                        legacyUnscoped: !sourceRole,
                        legacy_claim_player_id: ctx.player_id,
                    });
                }
            });
            /* eslint-enable security/detect-object-injection */
            progress.migrations.songMasteryV1 = {
                completed: true, claimed_by: claimedBy, claimed_player_id: ctx.player_id,
                source_retained: true,
                completed_at: new Date().toISOString(),
            };
            saveProgressStore(progress);
            flushProgressStore(); // one-time migration, not a hot gameplay path — persist immediately
        }

        var phraseStore = loadPhraseAttemptStore();
        if (!phraseStore.migrations.phraseAttemptsV1) {
            var legacyAttempts;
            try { legacyAttempts = JSON.parse(localStorage.getItem(PHRASE_ATTEMPTS_LS_KEY) || '[]'); }
            catch (_) { legacyAttempts = []; }
            if (!Array.isArray(legacyAttempts)) legacyAttempts = [];
            legacyAttempts.forEach(function (attempt, index) {
                if (!_plainObject(attempt)) return;
                var identity = _legacySongIdentity(attempt.song_key || attempt.song_id);
                if (!identity) return;
                var legacyId = 'phraseAttempts.v1:' + index + ':' + _id(attempt.session_id, 'unknown');
                var sourceInstrument = _id(attempt.instrument);
                var instrument = _legacyInstrumentForValue(sourceInstrument) || 'legacy-unknown';
                var sourceRole = _id(attempt.role);
                var attemptContext = Object.assign({}, ctx, identity, {
                    instrument: instrument,
                    role: sourceRole || _legacyRoleForInstrument(instrument),
                    skill: 'overall',
                });
                var attemptNode = _phraseAttemptNode(phraseStore, attemptContext, true);
                if (!attemptNode) return;
                if (attemptNode.attempts.some(function (item) { return item && item.legacy_id === legacyId; })) return;
                attemptNode.attempts.push(Object.assign({}, attempt, identity, {
                    schema: 'difficulty_ladder.phrase_attempt.v2',
                    legacy_id: legacyId,
                    player_id: ctx.player_id,
                    profile_id: ctx.profile_id,
                    profile_hash: ctx.profile_hash,
                    instrument: instrument,
                    role: sourceRole || _legacyRoleForInstrument(instrument),
                    skill: 'overall',
                    legacy_unscoped_instrument: !sourceInstrument || !sourceRole,
                    legacy_claim_player_id: ctx.player_id,
                }));
                attemptNode.attempts = attemptNode.attempts.slice(-MAX_PHRASE_ATTEMPTS);
            });
            phraseStore.migrations.phraseAttemptsV1 = {
                completed: true, claimed_by: claimedBy, claimed_player_id: ctx.player_id,
                source_retained: true,
                completed_at: new Date().toISOString(),
            };
            _phraseAttemptStore.markDirty();
            flushPhraseAttempts();
        }
        return true;
    }

    // A finalized phrase's mastery is the difficulty that was actually
    // presented (the live 0..100 slider) multiplied by its judged hit rate.
    // This keeps a clean phrase at 70% difficulty worth 70 mastery, while a
    // 50% result at that difficulty is worth 35. bestMastery is monotonic and
    // never changes the independently persisted currentDifficulty target.
    function _phraseMasteryPct(highway, ratio) {
        if (!highway || typeof highway.getMastery !== 'function') return null;
        var hitRate = Number(ratio);
        var difficulty = Number(highway.getMastery());
        if (!isFinite(hitRate) || !isFinite(difficulty)) return null;
        hitRate = Math.max(0, Math.min(1, hitRate));
        difficulty = Math.max(0, Math.min(1, difficulty));
        return Math.round(difficulty * 100 * hitRate * 100) / 100;
    }

    function _updateBestMastery(context, highway, ratio) {
        var ctx = normalizePlayerContext(context);
        var mastery = _phraseMasteryPct(highway, ratio);
        if (!ctx || mastery === null) return false;
        return writeProgress(ctx, { bestMastery: mastery });
    }

    // The shared ramp/commit step: fold one completed phrase into `state`'s
    // EMA and counters, then run the warm-up / down-confirm / manual-override
    // / ramp state machine both pipelines share. `completed` is either a hit
    // ratio (direct unit-test / settle-tool calls) or the ledger snapshot
    // `_advanceScorePhrase` reports; the snapshot's `ratio` drives the EMA
    // and ramp, while the snapshot's ledger fields drive the attempt record
    // -- so the record observes the completed phrase even though the state
    // has already reset for the incoming one. The snapshot is shaped like a
    // score state (`curPhraseIdx`/`phraseTotal`/`phraseHits`/`phraseJudgments`)
    // precisely so `recordPhraseAttempt` reads it through the same
    // `scoreState` param with no special case. The two callers differ ONLY in
    // the side channels around that machine -- captured in `hooks` -- which
    // is why the KNOWN DIFFERENCES the Stage 4-1 tests pin (write channel,
    // override scope, attempt scope, streak, diagnostics) live there and not
    // in the steps below:
    //   hooks.contextForAttempt/highwayForAttempt: where the phrase attempt
    //     and best-mastery records go (main: `_mainPlayerContext` +
    //     `window.highway`; split: `state.context` + the panel highway).
    //     `stateForAttempt` is vestigial -- both callers pass their own state
    //     and the snapshot path ignores it -- kept so the existing hook
    //     objects keep their shape. (Stage 4-4 #166: flagged for removal
    //     once the direct-ratio callers are migrated; do not add new uses.)
    //   hooks.recordStreak: main updates the mastery streak on every commit;
    //     split has no equivalent (Stage 4-1 DIFFERENCE test).
    //   hooks.onManualOverride(state, curPct): main disables auto-adjust
    //     GLOBALLY (setting + persisted + UI sync); split sets the panel's
    //     `manualOverride` flag only.
    //   hooks.applyStep(state, hw, curPct, step, direction, next): main writes
    //     via `window.setMastery` and records `lastAutoAction` on the state
    //     for diagnostics; split writes via the panel highway
    //     (`_applyDifficultyForContext` when the panel has a context, raw
    //     `hw.setMastery` for legacy untagged panels) with no auto-action
    //     record.
    //   hooks.afterCommit(state): main contributes a diagnostics payload on
    //     every return path; split contributes none. It reads the live
    //     default state directly -- no sync layer, since `_mainScore` IS the
    //     storage `contributeDiagnostics` already reads.
    // Returns nothing; all ramp progress lands in `state`.
    function _commitScoreRatio(state, completed, hw, hooks) {
        // A ledger snapshot carries the completed phrase's ledger (the state
        // has already reset for the incoming phrase); a bare ratio reads the
        // ledger off the state, as before. Either way the attempt record and
        // the EMA observe the SAME completed phrase.
        var snapshot = completed !== null && typeof completed === 'object' ? completed : null;
        var ratio = snapshot ? snapshot.ratio : completed;
        // Attempt scope stays a caller property, not the machine's: split
        // records only when the panel has a context (an untagged legacy
        // panel logs none); main always records one.
        var attemptState = snapshot || (hooks.stateForAttempt === undefined ? state : hooks.stateForAttempt);
        var attemptContext = hooks.contextForAttempt;
        if (hooks.recordAttempt === undefined ? !!attemptContext : hooks.recordAttempt) {
            recordPhraseAttempt(ratio, attemptContext, attemptState, hooks.highwayForAttempt || hw);
        }
        _updateBestMastery(hooks.contextForAttempt, hw, ratio);
        var alpha = emaAlpha();
        state.emaHitRate = (state.emaHitRate == null)
            ? ratio : (alpha * ratio + (1 - alpha) * state.emaHitRate);
        // Counts every phrase actually played this song, regardless of
        // autoAdjust -- a warm-up satisfied while paused should still count
        // once the user flips auto-adjust back on, rather than resetting.
        state.phrasesScored++;
        if (hooks.recordStreak) {
            updateMasteryStreak(ratio, hw && typeof hw.getMastery === 'function'
                ? Math.round(hw.getMastery() * 100) : null);
        }
        if (!settings.autoAdjust || state.manualOverride || !hw || typeof hw.getMastery !== 'function') {
            state.downStreak = 0;
            state.rampProgress = 0;
            // Main has no `manualOverride` gate (its field stays false), so
            // `state.manualOverride` here is split-only -- but clearing the
            // ramp counters on this path is shared: main did exactly this on
            // its own early returns. Main additionally clears `rampDirection`
            // on early returns; split never had a `rampDirection` clear here
            // (it returns with the field untouched). That asymmetry is
            // preserved below via `hooks.clearRampDirectionOnEarlyReturn`.
            if (hooks.clearRampDirectionOnEarlyReturn) state.rampDirection = null;
            if (hooks.afterCommit) hooks.afterCommit(state);
            return;
        }
        // Cold-start guard: don't let a single nervous/rusty first section on
        // a fresh song swing the slider before there's enough signal.
        if (state.phrasesScored < WARMUP_PHRASES) {
            if (hooks.afterCommit) hooks.afterCommit(state);
            return;
        }

        var curPct = Math.round(hw.getMastery() * 100);
        // The compatibility API cannot identify who originated a mastery
        // change. Treat originless drift conservatively as a possible manual
        // override and stand down; this is a safety heuristic, not proof that
        // a person moved the slider.
        if (state.lastObservedMasteryPct != null && curPct !== state.lastObservedMasteryPct) {
            state.rampDirection = null;
            state.rampProgress = 0;
            state.downStreak = 0;
            hooks.onManualOverride(state, curPct);
            if (hooks.afterCommit) hooks.afterCommit(state);
            return;
        }

        var th = thresholds();
        var direction = rampDirection(state.emaHitRate, th);
        state.downStreak = direction === 'down' && settings.dropResistance ? state.downStreak + 1 : 0;
        if (direction == null) {
            state.rampDirection = null;
            state.rampProgress = 0;
            if (hooks.afterCommit) hooks.afterCommit(state);
            return;
        }
        state.lastObservedMasteryPct = curPct;
        if (direction === 'down' && settings.dropResistance && state.downStreak < DOWN_CONFIRM_PHRASES) {
            state.rampDirection = null;
            state.rampProgress = 0;
            if (hooks.afterCommit) hooks.afterCommit(state);
            return;
        }
        if (state.rampDirection !== direction) {
            state.rampDirection = direction;
            state.rampProgress = 0;
        }
        var step = rampStep(th, state.rampProgress, direction);
        var next = direction === 'up' ? curPct + step : curPct - step;
        next = Math.max(settings.minMastery, Math.min(settings.maxMastery, next));

        if (hooks.applyStep) hooks.applyStep(state, hw, curPct, step, direction, next);
        if (hooks.afterCommit) hooks.afterCommit(state);
    }

    // Main-player hooks for the shared commit step: the default state's
    // side channels. `contextForAttempt` is read live (not captured) so a
    // mid-song context switch commits to the current context.
    function _mainCommitHooks() {
        return {
            get contextForAttempt() { return _mainPlayerContext; },
            stateForAttempt: _mainScore,
            highwayForAttempt: null,
            recordStreak: true,
            clearRampDirectionOnEarlyReturn: true,
            onManualOverride: function () {
                settings.autoAdjust = false;
                lsSet('autoAdjust', false);
                syncControlsUI();
            },
            applyStep: function (state, hw, curPct, step, direction, next) {
                if (next !== curPct && typeof window.setMastery === 'function') {
                    window.setMastery(next);
                    state.lastObservedMasteryPct = next;
                    state.rampProgress = (state.rampProgress + 1) % RAMP_PHRASES;
                    var dir = next > curPct ? 'up' : 'down';
                    state.lastAutoAction = {
                        direction: dir,
                        pct: next,
                        step: step,
                        reason: dir === 'up' ? 'ema_above_up_threshold' : 'ema_below_down_threshold',
                    };
                }
            },
            // Reads the live default state directly -- no sync layer, since
            // `_mainScore` IS the storage `contributeDiagnostics` already
            // reads.
            afterCommit: function () { contributeDiagnostics(); },
        };
    }

    function commitPhraseResult(ratio) {
        var hw = window.highway;
        // The main player IS the default state (#164): the shared machine
        // runs against `_mainScore` directly, so every other reader --
        // `recordPhraseAttempt` (via `stateForAttempt`), `contributeDiagnostics`
        // (via the `afterCommit` hook) -- observes the live values.
        _commitScoreRatio(_mainScore, ratio, hw, _mainCommitHooks());
    }

    function updateMasteryStreak(ratio, masteryPct) {
        var maxPct = Number(settings.maxMastery);
        if (!isFinite(maxPct)) maxPct = 100;
        maxPct = Math.max(0, Math.min(100, maxPct));
        if (typeof masteryPct === 'number' && isFinite(masteryPct)
            && masteryPct >= maxPct && ratio >= MASTERY_STREAK_ACCURACY) {
            _masteryStreak++;
        } else {
            _masteryStreak = 0;
        }
        return _masteryStreak;
    }

    function resetMasteryStreak() {
        _masteryStreak = 0;
    }

    function masteryStreakStatus() {
        return { count: _masteryStreak, active: _masteryStreak >= MASTERY_STREAK_PHRASES };
    }

    function contributeDiagnostics() {
        var fb = window.feedBack;
        if (!fb || !fb.diagnostics || typeof fb.diagnostics.contribute !== 'function') return;
        var hw = window.highway;
        var provider = hw && typeof hw.getNoteStateProvider === 'function' ? hw.getNoteStateProvider() : null;
        fb.diagnostics.contribute(PLUGIN_ID, {
            schema: 'difficulty_ladder.v1',
            ema_hit_rate: _mainScore.emaHitRate,
            last_auto_action: _mainScore.lastAutoAction,
            provider_registered: !!provider,
            auto_adjust_enabled: settings.autoAdjust,
            show_difficulty_guide: settings.showDifficultyGuide,
            phrase_attempt_log: {
                storage_key: PHRASE_ATTEMPTS_V2_LS_KEY,
                schema: 'difficulty_ladder.phrase_attempt.v2',
                retained_attempts: loadPhraseAttempts(_mainPlayerContext).length,
                max_retained_attempts: MAX_PHRASE_ATTEMPTS,
            },
        });
    }

    // Calculate and emit section difficulty data for other plugins (e.g., sectionmap)
    function calculateAndEmitSectionDifficulties(explicitContext, explicitHighway) {
        var context = normalizePlayerContext(explicitContext || _mainPlayerContext);
        var hw = explicitHighway || (context && context.highway)
            || (!context ? window.highway : null);
        var fb = window.feedBack;

        // Early exit if dependencies aren't available
        if (!hw || typeof hw.getSections !== 'function' || !fb || typeof fb.emit !== 'function') return;
        if (typeof hw.getPhrases !== 'function' || !hw.getPhrases()) return;

        var sections = hw.getSections();
        var phrases = hw.getPhrases();
        var reportsMastery = typeof hw.getMastery === 'function';
        // 0.5 is v2's long-standing stand-in for a highway that reports no
        // mastery at all; it is kept for v2 alone below so that frozen payload
        // is unchanged. v3 gets what the host actually said (null), or the
        // fallback would read as a real measurement — the same verdict
        // _presentedDifficultyLevel() reaches for the HUD.
        var mastery = reportsMastery ? hw.getMastery() : 0.5;

        if (!sections || sections.length === 0 || !phrases || phrases.length === 0) return;

        // Calculate max difficulty across all phrases
        var maxDiff = 1;
        phrases.forEach(function (phrase) {
            maxDiff = Math.max(maxDiff, _phraseTopDifficulty(phrase));
        });

        // Map sections to difficulty data
        var sectionDifficulties = {};
        for (var si = 0; si < sections.length; si++) {
            var section = sections[si];
            var nextSectionTime = si < sections.length - 1 ? sections[si + 1].time : Infinity;

            // Find phrases within this section's time range
            var sectionDifficultiesInRange = [];
            var hardestPhrase = null;
            for (var pi = 0; pi < phrases.length; pi++) {
                var phrase = phrases[pi];
                // Check if phrase overlaps with section
                if (phrase.end_time > section.time && phrase.start_time < nextSectionTime) {
                    var top = _phraseTopDifficulty(phrase);
                    sectionDifficultiesInRange.push(top);
                    if (!hardestPhrase || top > _phraseTopDifficulty(hardestPhrase)) hardestPhrase = phrase;
                }
            }

            if (sectionDifficultiesInRange.length > 0) {
                // Use the average difficulty in this section
                var avgDifficulty = sectionDifficultiesInRange.reduce(function(a, b) { return a + b; }, 0) / sectionDifficultiesInRange.length;
                var maxSectionDifficulty = Math.max.apply(Math, sectionDifficultiesInRange);

                // Issue #63: same discrete tier formula drawHud() uses for its
                // own per-phrase glasses, applied to this section's aggregate
                // (max-of-overlapping-phrases) difficulty -- previously this
                // used a different, continuous mastery-scaled fraction here,
                // which could disagree materially with drawHud()'s discrete
                // tiers for the same mastery/difficulty pair.
                //
                // maxSectionDifficulty === 0 means every overlapping phrase
                // is a single level. That is two different things: an empty/
                // silent section (no chart content at all) must not show a
                // misleadingly "fully mastered" glass (0%, review-caught --
                // Sourcery, PR #79), but a section of easy phrases that are
                // complete at the bottom tier (generated ladders on the
                // shared tier scale) is played in full at every slider
                // position -- 100%, matching drawHud's full glass for the
                // same phrases. The full chart's own notes tell them apart.
                var fillPercentage;
                if (maxSectionDifficulty > 0) {
                    fillPercentage = _tierFillFrac(mastery, Number(hardestPhrase.max_difficulty),
                        maxSectionDifficulty).fillFrac * 100;
                } else {
                    fillPercentage = _chartHasContentIn(hw, section.time, nextSectionTime) ? 100 : 0;
                }

                // Determine glass size based on section difficulty
                var glassSize = 'medium';
                if (maxSectionDifficulty < maxDiff * 0.33) glassSize = 'small';
                else if (maxSectionDifficulty > maxDiff * 0.66) glassSize = 'large';

                sectionDifficulties[si] = {
                    fillPercentage: fillPercentage,
                    glassSize: glassSize,
                    avgDifficulty: avgDifficulty,
                    maxDifficulty: maxSectionDifficulty,
                };
            }
        }

        // Emit event for sectionmap and other interested plugins
        var playerContextPayload = _contextEventPayload(context);
        fb.emit(SECTIONS_EVENT_V2, {
            schema: SECTIONS_SCHEMA_V2,
            player_context: playerContextPayload,
            sectionDifficulties: sectionDifficulties,
            mastery: mastery,
            maxDifficulty: maxDiff,
        });

        // The render-neutral v3 payload (issue #156) rides this same call and
        // the same sections/phrases pass, so it costs one extra payload build
        // per emit — no extra cadence, no per-note scanning beyond the content
        // probe above. Its only additional highway reads are getTime() and
        // getSongInfo(), which the is_current / current_phrase_index and
        // joinable phrase id fields are for.
        fb.emit(SECTIONS_EVENT_V3,
            _sectionsV3Payload(sections, phrases, hw,
                reportsMastery ? mastery : null, playerContextPayload));
    }

    // ---- difficulty_ladder.sections.v3 (issue #156) ----------------------
    // The render-neutral twin of the v2 payload above: same aggregation over
    // the same highway data, but every field states what the data *is* rather
    // than how a glass should be drawn, so a consumer can render bars, rings,
    // a heat map or nothing from one payload. See INTEGRATION.md ->
    // "sections v3" for the full contract; each section and phrase entry
    // carries:
    //   id / index      - stable key. A phrase's id is the same string
    //                     phrase_attempt.v2 records are keyed by, so a consumer
    //                     can join the two without a translation table.
    //   start/end_time  - half-open [start_time, end_time), matching
    //                     drawHud()'s and tickScoring()'s phrase cursor.
    //                     end_time is null on an open-ended final section.
    //   current_tier    - the tier this player's mastery currently maps to, or
    //                     null when mastery isn't reported. Never a fabricated
    //                     0 for missing data.
    //   top_tier        - the tier from which the entry plays in full
    //                     (core's top_difficulty, falling back to max_difficulty).
    //                     0 means the phrase reports no ladder at all (a
    //                     single-level or hand-authored phrase), which is the
    //                     same reading v2 gives it.
    //   max_tier        - the top of that entry's own tier ladder (max_difficulty).
    //   is_current      - whether the entry covers the playback time as of this
    //                     emit. Emits are event-driven, so this is a snapshot
    //                     that goes stale until the next mastery or difficulty
    //                     event; a renderer following the playhead reads
    //                     highway.getTime() itself against start_time/end_time
    //                     (INTEGRATION.md -> "Freshness").
    //   has_chart_content - whether the span holds playable notes/chords: probed
    //                     per section, inherited by a single-level phrase from
    //                     its section (see the note at the assignment).
    // A section with no overlapping phrase gets no entry at all, same as v2:
    // absent means "nothing to show", never "tier 0".
    function _sectionsV3Payload(sections, phrases, hw, mastery, contextPayload) {
        // Everything the entries below read from the host, read once per emit.
        // mastery and time are null when the host doesn't report them, which is
        // why they are coerced here rather than at each use.
        var state = {
            hw: hw,
            mastery: _v3NumberOrNull(mastery),
            time: _v3NumberOrNull(typeof hw.getTime === 'function' ? hw.getTime() : null),
            songKey: _v3PhraseSongKey(contextPayload, hw),
        };
        return {
            schema: SECTIONS_SCHEMA_V3,
            player_context: contextPayload,
            mastery: state.mastery,
            max_tier: phrases.reduce(function (top, phrase) {
                return Math.max(top, _v3TierRange(phrase).max_tier);
            }, 0),
            current_phrase_index: _v3CurrentPhraseIndex(phrases, state.time),
            sections: _v3SectionEntries(sections, phrases, state),
        };
    }

    // One entry per section that overlaps a phrase, in order.
    function _v3SectionEntries(sections, phrases, state) {
        var entries = [];
        for (var si = 0; si < sections.length; si++) {
            // The last section has no successor to bound it; Infinity is the
            // host's own convention here (v2 passes it the same way) but a
            // payload cannot carry it, so it is emitted as null.
            var endTime = si < sections.length - 1 ? sections[si + 1].time : null;
            var entry = _v3SectionEntry(si, sections[si].time, endTime, phrases, state);
            if (entry) entries.push(entry);
        }
        return entries;
    }

    // One section's entry, or null when no phrase overlaps it (the omission
    // rule stated above). Same aggregation rule as v2 — hardest overlapping
    // phrase by top_tier, mean of the section's top_tiers — restated as tiers.
    function _v3SectionEntry(index, startTime, endTime, phrases, state) {
        var endBoundary = endTime == null ? Infinity : endTime;
        var overlapping = _v3Overlapping(phrases, startTime, endBoundary);
        if (overlapping.entries.length === 0) return null;

        var hardest = overlapping.hardest;
        var topTier = hardest.tiers.top_tier;
        // top_tier === 0 means every overlapping phrase is single-level,
        // which is two different things: a section of easy phrases that is
        // played in full at every slider position, and a silent section.
        // The full chart's own notes tell them apart — the same
        // distinction v2 draws as 100% vs 0% fill (Sourcery, PR #79),
        // expressed as data instead of as a percentage.
        var hasChartContent = topTier > 0 || _chartHasContentIn(state.hw, startTime, endBoundary);

        return {
            id: 'section:' + index,
            index: index,
            start_time: startTime,
            end_time: endTime,
            is_current: _v3Covers(state.time, startTime, endBoundary),
            current_tier: state.mastery === null ? null
                : _v3CurrentTier(state.mastery, hardest.tiers.max_tier, topTier),
            top_tier: topTier,
            max_tier: overlapping.max_tier,
            avg_top_tier: overlapping.top_tier_sum / overlapping.entries.length,
            has_chart_content: hasChartContent,
            phrases: overlapping.entries.map(function (item) {
                return _v3PhraseEntry(item, state, hasChartContent);
            }),
        };
    }

    // The phrases overlapping one section's [startTime, endBoundary) span, each
    // with its own tier range, plus the three aggregates its entry reports: the
    // hardest of them (the first reaching the top top_tier, as v2's rule reads),
    // the top of their ladders, and the sum their mean is taken from.
    function _v3Overlapping(phrases, startTime, endBoundary) {
        var entries = [];
        var hardest = null;
        var maxTier = 0;
        var topTierSum = 0;
        for (var pi = 0; pi < phrases.length; pi++) {
            var phrase = phrases[pi];
            if (!(phrase.end_time > startTime && phrase.start_time < endBoundary)) continue;
            var tiers = _v3TierRange(phrase);
            var entry = { index: pi, phrase: phrase, tiers: tiers };
            entries.push(entry);
            maxTier = Math.max(maxTier, tiers.max_tier);
            topTierSum += tiers.top_tier;
            if (hardest === null || tiers.top_tier > hardest.tiers.top_tier) hardest = entry;
        }
        return { entries: entries, hardest: hardest, max_tier: maxTier, top_tier_sum: topTierSum };
    }

    function _v3PhraseEntry(item, state, hasChartContent) {
        return {
            id: _phraseIdOf(state.songKey, item.index, item.phrase) || 'phrase:' + item.index,
            index: item.index,
            start_time: item.phrase.start_time,
            end_time: item.phrase.end_time,
            is_current: _v3Covers(state.time, item.phrase.start_time, item.phrase.end_time),
            current_tier: state.mastery === null ? null
                : _v3CurrentTier(state.mastery, item.tiers.max_tier, item.tiers.top_tier),
            top_tier: item.tiers.top_tier,
            max_tier: item.tiers.max_tier,
            // A phrase with a ladder is taken to have content; a
            // single-level one inherits its section's verdict. Probing
            // every phrase's own span would make this emit O(phrases x
            // notes) for no extra fact a renderer needs.
            has_chart_content: item.tiers.top_tier > 0 || hasChartContent,
        };
    }

    // Its own pass over the phrases rather than a side effect of building the
    // section entries: a phrase can be current while overlapping no section it
    // would be reported under.
    function _v3CurrentPhraseIndex(phrases, playbackTime) {
        if (playbackTime === null) return null;
        for (var i = 0; i < phrases.length; i++) {
            if (_v3Covers(playbackTime, phrases[i].start_time, phrases[i].end_time)) return i;
        }
        return null;
    }

    // Whether the playback cursor sits in the half-open [start, end) span. A
    // null time is inside no span, so an unreported time marks nothing current
    // instead of landing on the first entry.
    function _v3Covers(playbackTime, start, end) {
        return playbackTime !== null && playbackTime >= start && playbackTime < end;
    }

    // null, '' and anything non-numeric all mean "not reported". Recognized
    // before the value reaches the tier math: Number(null) === 0 and
    // Number('') === 0, so a coerced one would read as a real "played at the
    // bottom tier" measurement.
    function _v3NumberOrNull(value) {
        if (value == null || value === '') return null;
        var n = Number(value);
        return isFinite(n) ? n : null;
    }

    // An entry's own ladder: max_tier is the top of its tier scale, top_tier
    // the tier from which it is played in full. top_tier is clamped to max_tier
    // so a malformed pair can't report "full detail" above the ladder's top.
    function _v3TierRange(phrase) {
        var maxTier = Number(phrase && phrase.max_difficulty);
        if (!isFinite(maxTier) || maxTier < 0) maxTier = 0;
        return { max_tier: maxTier, top_tier: Math.min(_phraseTopDifficulty(phrase), maxTier) };
    }

    // The song/arrangement key a v3 phrase entry is identified by. It is
    // deliberately the one recordPhraseAttempt() builds from the player
    // context (`song_id::arrangement_id`) rather than songKeyOf(highway
    // song info), because those are the ids phrase_attempt.v2 records are
    // stored under and a consumer can only join the two if they agree. Without
    // a context (a legacy main-player emission) the highway's own song info is
    // the next best thing; songKeyOf({}) is the truthy '::' rather than null,
    // so an empty song info is rejected here too and the entry falls back to
    // its index.
    function _v3PhraseSongKey(contextPayload, hw) {
        if (contextPayload && contextPayload.song_id) {
            return contextPayload.song_id + '::' + (contextPayload.arrangement_id || '');
        }
        var songKey = songKeyOf(typeof hw.getSongInfo === 'function' ? hw.getSongInfo() : null);
        return songKey === '::' ? null : songKey;
    }

    // The tier a mastery value currently maps to, from the same discrete
    // ladder every other consumer here uses (_tierFillFrac), clamped to
    // `playedFullFrom`: above that tier the entry is already played in full,
    // so a larger number would only invite a renderer to draw detail that
    // cannot be added.
    //
    // A zero tier on either axis is the "no ladder reported" reading top_tier
    // and max_tier carry, not a measurement at the bottom one, and
    // _tierFillFrac's zero-ladder convention answers index 0 — which would
    // fabricate the bottom tier for exactly the entries (silent, single-level
    // or hand-authored) that report no ladder. null says "no tier to draw".
    function _v3CurrentTier(mastery, ladderTier, playedFullFrom) {
        if (!isFinite(ladderTier) || ladderTier <= 0) return null;
        if (!isFinite(playedFullFrom) || playedFullFrom <= 0) return null;
        var tier = _tierFillFrac(mastery, ladderTier, playedFullFrom).idxLevel;
        if (!isFinite(tier)) return null;
        return Math.max(0, Math.min(playedFullFrom, tier));
    }

    // Coalesces rapid-fire calculateAndEmitSectionDifficulties() calls (e.g.
    // the mastery slider's oninput firing per pixel dragged) into at-most-
    // once-per-150ms. Unlike lsSetDebounced above (a restart-on-call
    // debounce that only fires once, after the last call), this is a
    // fire-once-then-wait trailing throttle: it keeps firing roughly every
    // 150ms throughout a continuous drag rather than waiting for it to end.
    // Deliberate — Section Map's difficulty display should update live
    // during a drag, not only once the user lets go.
    var _sectionDiffEmitTimers = new Map();
    function _sectionDiffTimerKey(context) {
        return playerContextKey(context) || 'main';
    }
    function scheduleSectionDifficultiesEmit(context, highway) {
        var key = _sectionDiffTimerKey(context);
        if (_sectionDiffEmitTimers.has(key)) return; // each pane throttles independently
        var handle = setTimeout(function () {
            _sectionDiffEmitTimers.delete(key);
            calculateAndEmitSectionDifficulties(context, highway);
        }, 150);
        _sectionDiffEmitTimers.set(key, handle);
    }
    function _cancelSectionDifficultiesForPlayer(key) {
        if (!_sectionDiffEmitTimers.has(key)) return;
        clearTimeout(_sectionDiffEmitTimers.get(key));
        _sectionDiffEmitTimers.delete(key);
    }
    function cancelSectionDifficultiesEmit(context) {
        if (context) {
            _cancelSectionDifficultiesForPlayer(_sectionDiffTimerKey(context));
            return;
        }
        _sectionDiffEmitTimers.forEach(function (handle) { clearTimeout(handle); });
        _sectionDiffEmitTimers.clear();
    }

    // Reads live per-note judgments through the note-state provider slot
    // (owned by whichever scorer plugin, e.g. note_detect, is active). This
    // is a read, not a takeover — highway.getNoteStateProvider() is a public
    // getter documented for exactly this kind of consumption.
    var _scoreRafHandle = null;
    var PENDING_POLL_INTERVAL_SECONDS = 0.1;
    var FORWARD_DISCONTINUITY_SECONDS = 1;

    function _scoringWallTimeSeconds() {
        return typeof performance !== 'undefined' && typeof performance.now === 'function'
            ? performance.now() / 1000
            : Date.now() / 1000;
    }

    function _isForwardScoringDiscontinuity(previousT, currentT, previousWallT, currentWallT) {
        if (previousT < 0 || previousWallT < 0) return false;
        var playbackAdvance = currentT - previousT;
        var wallAdvance = Math.max(0, currentWallT - previousWallT);
        // A stalled/throttled frame advances playback and wall time together.
        // Only excess playback movement indicates a seek without an explicit
        // Host seek-origin event.
        return playbackAdvance > wallAdvance + FORWARD_DISCONTINUITY_SECONDS;
    }

    function _advanceCursorToTime(items, cursor, playbackTime) {
        // items[cursor] is a non-negative integer cursor bounded by items.length
        // above, never external/attacker-controlled input.
        /* eslint-disable security/detect-object-injection */
        while (cursor < items.length && items[cursor].t < playbackTime) cursor++;
        /* eslint-enable security/detect-object-injection */
        return cursor;
    }

    // ---- Shared scoring state machine (Stage 4-2 #164, Stage 4-3 #165) ----
    // One parameterized judgment-polling/commit machine for the main player
    // and Split Screen panels. The main player is the
    // DEFAULT STATE: `_mainScore` holds the same fields a per-panel split
    // state holds, and every shared step below reads/writes the state object
    // it is handed -- never the module-level `let` bindings directly.
    // `_mainScore` IS the main player's storage (replacing the old
    // per-field `let` bindings); `resetPerSongState`, `contributeDiagnostics`
    // and the rest of the file read it directly, so there is no sync layer
    // and no way for the two sides to disagree.
    //
    // Field parity with `_resetSplitScoreState` is load-bearing, not
    // cosmetic: `_mainScore` must carry every field the shared steps touch,
    // including the ones the main path previously had no use for (`context`,
    // `playerKey`, `manualOverride` live on the default state too, holding
    // null/false). A field added to one initializer and not the other is a
    // divergence the characterization tests may not catch, since they only
    // observe committed outcomes. `_mainScore` is assigned ONCE at load (the
    // declaration above); `_resetMainScoreState` only refreshes its fields
    // in place, so a reference handed out earlier stays valid.

    // Reset the main player's score state for a new song. Split panel states
    // reset through `_resetSplitScoreState`, never here -- this is main-only.
    function _resetMainScoreState() {
        _mainScore.judgedKeys = new Set();
        _mainScore.pendingJudgments = new Map();
        _mainScore.phraseHits = 0;
        _mainScore.phraseTotal = 0;
        _mainScore.phraseJudgments = [];
        _mainScore.phrasesScored = 0;
        _mainScore.curPhraseIdx = -1;
        _mainScore.lastScoredT = -1;
        _mainScore.lastScoredWallT = -1;
        _mainScore.noteCursor = 0;
        _mainScore.chordCursor = 0;
        _mainScore.emaHitRate = null;
        _mainScore.lastObservedMasteryPct = null;
        _mainScore.lastAutoAction = null;
        _mainScore.rampDirection = null;
        _mainScore.rampProgress = 0;
        _mainScore.downStreak = 0;
        _mainScore.manualOverride = false;
        _mainScore.context = null;
        _mainScore.playerKey = null;
    }

    // The shared enqueue step: feed one highway's time-sorted notes/chords
    // into `state`'s pending ledger up to `cutoff`. Same call for the main
    // path (state = `_mainScore`) and each split panel (state = the
    // per-highway object) -- the only difference is whose cursors advance.
    // Highway-array reads stay INSIDE this step (one fetch per call) so the
    // per-frame path fetches each array exactly once per pass.
    function _enqueueScoreEvents(hw, state, phrase, cutoff) {
        var notes = typeof hw.getFilteredNotes === 'function' ? hw.getFilteredNotes() : [];
        var chords = typeof hw.getFilteredChords === 'function' ? hw.getFilteredChords() : [];
        state.noteCursor = _enqueuePhraseJudgments(
            notes, state.noteCursor, phrase, cutoff, function (n) { return [n]; },
            state.pendingJudgments, state.judgedKeys
        );
        state.chordCursor = _enqueuePhraseJudgments(
            chords, state.chordCursor, phrase, cutoff, function (c) { return c.notes || []; },
            state.pendingJudgments, state.judgedKeys
        );
    }

    // The shared poll step: resolve `state`'s pending ledger against
    // `provider`, folding terminal hit/miss verdicts into the in-flight
    // phrase totals. Same `onTerminal` shape on both paths -- the ledgers
    // are structurally identical (`phraseTotal`/`phraseHits`/`phraseJudgments`
    // with `key`/`time`/`string`/`fret`/`hit` entries), which is what lets
    // `recordPhraseAttempt` read either one through its `scoreState` param.
    function _pollScorePending(state, provider, playbackTime, force) {
        _pollPendingJudgments(
            state.pendingJudgments, state.judgedKeys, provider, playbackTime, force,
            function (entry, hit) {
                state.phraseTotal++;
                if (hit) state.phraseHits++;
                state.phraseJudgments.push({
                    key: entry.key, time: entry.time, string: entry.note.s,
                    fret: entry.note.f, hit: hit,
                });
            }
        );
    }

    // The shared cursor-advance step: skip every event crossed by a forward
    // seek before resuming normal cursor-fed scoring at the destination.
    // Advances the passed state's cursors over already-fetched arrays;
    // `_advanceCursorsToTime` below is the highway-reading entry point (one
    // fetch per call -- the chart can change under a seek).
    function _advanceScoreCursors(state, notes, chords, playbackTime) {
        state.noteCursor = _advanceCursorToTime(notes, state.noteCursor, playbackTime);
        state.chordCursor = _advanceCursorToTime(chords, state.chordCursor, playbackTime);
    }

    // Drop the in-flight phrase's judgments (a replay must judge fresh; a
    // forward jump must not fabricate judgments for its unplayed tail).
    // Factored because the discontinuity step needs it on two branches;
    // cursors are NOT reset here -- a backward seek resyncs them from
    // scratch at the call site, a forward jump advances past the jump via
    // `_advanceCursorsToTime`.
    function _abandonScorePhrase(state) {
        state.phraseHits = 0;
        state.phraseTotal = 0;
        state.phraseJudgments = [];
        state.judgedKeys = new Set();
        state.pendingJudgments = new Map();
    }

    // The shared discontinuity step: fold one frame's seek handling into
    // `state` and report whether the frame's phrase work should be skipped.
    // Backward and forward seeks both abandon the in-flight phrase's
    // judgments (a replay must judge fresh; a forward jump must not
    // fabricate judgments for its unplayed tail); the caller advances
    // cursors past the jump via `_advanceScoreCursors` when `jumpedForward`
    // comes back true. Returns `{ rewound, jumpedForward }`.
    function _updateScoreDiscontinuity(state, t, wallT) {
        var rewound = false;
        var jumpedForward = _isForwardScoringDiscontinuity(
            state.lastScoredT, t, state.lastScoredWallT, wallT
        );
        // A backward jump (loop restart, user seek, section-practice rewind)
        // invalidates the forward-only cursors -- resync from scratch. This
        // branch is the only O(N)-ish path here and it's seek-triggered,
        // not per-frame.
        if (t < state.lastScoredT - 0.05) {
            rewound = true;
            state.noteCursor = 0;
            state.chordCursor = 0;
            // A seek within the same phrase leaves judgedKeys/phraseHits/etc
            // stale -- without this, replayed notes are skipped as already
            // judged (their keys are still in the set) and multiple passes
            // over the same phrase silently merge into one attempt.
            _abandonScorePhrase(state);
        }
        if (jumpedForward) {
            _abandonScorePhrase(state);
        }
        state.lastScoredT = t;
        state.lastScoredWallT = wallT;
        return { rewound: rewound, jumpedForward: jumpedForward };
    }

    // The shared phrase-transition step: locate the phrase covering `t`,
    // and when it differs from the state's current one, collect the old
    // phrase's tail (enqueue with an infinite cutoff, one final forced
    // poll, then discard whatever is still unresolved so it cannot leak
    // into the next phrase) and report the completed ledger for the caller
    // to commit. Returns `{ idx, completed }` where `completed` is null
    // when nothing should commit (rewound/jumped-forward frame, first
    // sighting, gap between phrases, or an empty phrase) and otherwise a
    // snapshot `{ ratio, curPhraseIdx, phraseTotal, phraseHits, phraseJudgments }`
    // taken BEFORE the state resets for the incoming phrase -- the commit
    // (attempt record included) must observe the completed phrase's ledger,
    // not the reset one. The state is always left reset for the incoming
    // phrase, committed or not.
    function _advanceScorePhrase(hw, state, phrases, t, provider, flags) {
        var rewound = flags && flags.rewound;
        var jumpedForward = flags && flags.jumpedForward;
        var idx = state.curPhraseIdx;
        if (idx < 0 || t < phrases[idx].start_time || t >= phrases[idx].end_time) {
            idx = phrases.findIndex(function (p) { return t >= p.start_time && t < p.end_time; });
        }
        var completed = null;
        if (idx !== state.curPhraseIdx) {
            // Collect even the tail that has not reached the normal maturity
            // delay, give every pending result one final poll, then explicitly
            // discard unresolved entries so they cannot leak into the next
            // phrase.
            if (!rewound && !jumpedForward && state.curPhraseIdx >= 0) {
                // eslint-disable-next-line security/detect-object-injection -- curPhraseIdx is bounded by the >= 0 check above and phrases.length
                _enqueueScoreEvents(hw, state, phrases[state.curPhraseIdx], Infinity);
                _pollScorePending(state, provider, t, true);
                state.pendingJudgments.clear();
            }
            if (!rewound && !jumpedForward && state.curPhraseIdx >= 0 && state.phraseTotal > 0) {
                completed = {
                    ratio: state.phraseHits / state.phraseTotal,
                    curPhraseIdx: state.curPhraseIdx,
                    phraseTotal: state.phraseTotal,
                    phraseHits: state.phraseHits,
                    phraseJudgments: state.phraseJudgments.slice(),
                };
            }
            state.curPhraseIdx = idx;
            _abandonScorePhrase(state);
        }
        return { idx: idx, completed: completed };
    }

    // Feed time-sorted song events into a phrase-local pending ledger exactly
    // once. The cursor advances when an event is enqueued, not when its scorer
    // result settles, so an 'active'/null result can remain pending for as long
    // as necessary without rescanning the song arrays.
    function _enqueuePhraseJudgments(items, cursor, phrase, cutoff, notesOf, pending, judged) {
        /* eslint-disable security/detect-object-injection --
           cursor and ni are non-negative integer indices bounded by the
           arrays' own .length, never external/attacker-controlled input. */
        while (cursor < items.length) {
            var item = items[cursor];
            if (item.t < phrase.start_time) {
                cursor++;
                continue;
            }
            if (item.t >= phrase.end_time || item.t > cutoff) break;
            var itemNotes = notesOf(item);
            for (var ni = 0; ni < itemNotes.length; ni++) {
                var note = itemNotes[ni];
                var key = judgmentKey(item.t, note.s, note.f);
                if (!judged.has(key) && !pending.has(key)) {
                    pending.set(key, {
                        key: key, note: note, time: item.t,
                        nextPollAt: -Infinity,
                    });
                }
            }
            cursor++;
        }
        /* eslint-enable security/detect-object-injection */
        return cursor;
    }

    function _pollPendingJudgments(pending, judged, provider, playbackTime, force, onTerminal) {
        pending.forEach(function (entry, key) {
            if (!force && playbackTime < entry.nextPollAt) return;
            var result = provider(entry.note, entry.time);
            var name = typeof result === 'string' ? result : result && result.state;
            if (name !== 'hit' && name !== 'miss') {
                entry.nextPollAt = playbackTime + PENDING_POLL_INTERVAL_SECONDS;
                return;
            }
            pending.delete(key);
            if (judged.has(key)) return;
            judged.add(key);
            onTerminal(entry, name === 'hit');
        });
    }

    function tickScoring() {
        if (!isPlayerActive()) {
            resetMasteryStreak();
            _scoreRafHandle = null;
            return;
        }
        _scoreRafHandle = requestAnimationFrame(tickScoring);

        // Split Screen owns separate highway instances and intentionally
        // suppresses the main-player detector.  Score those instances here,
        // with state isolated per panel, before following the normal main
        // highway path below.
        tickSplitScoring();

        var hw = window.highway;
        if (!hw || typeof hw.hasPhraseData !== 'function' || !hw.hasPhraseData()) return;

        _tickScoreHighway(hw, _mainScore, commitPhraseResult);
    }

    // The shared per-frame tick (#165): one judgment-polling/commit pass over
    // a single highway, parameterized by the score `state` and the commit
    // function. The main player calls it with the default state and
    // `commitPhraseResult`; each Split Screen panel calls it (via
    // `tickOneSplitHighway`) with its per-highway state and a bound
    // `commitSplitPhraseResult`. Guards, seek handling, phrase transitions
    // and the 0.6s maturity lookback are identical -- the only per-caller
    // difference is whose state advances and which commit side channels run.
    function _tickScoreHighway(hw, state, commit) {
        var provider = _scoreProvider(hw);
        if (!provider) return; // no active scorer — nothing to react to yet
        var phrases = hw.getPhrases();
        if (!phrases || phrases.length === 0) return;
        var t = hw.getTime();
        var wallT = _scoringWallTimeSeconds();
        // With no seek-origin metadata, a large playback-time gap is treated
        // conservatively as a forward seek. Abandon the in-flight phrase so
        // boundary collection cannot fabricate judgments for its unplayed tail.
        var discontinuity = _updateScoreDiscontinuity(state, t, wallT);
        var rewound = discontinuity.rewound;
        var jumpedForward = discontinuity.jumpedForward;
        if (jumpedForward) {
            // Seek-only scan: skip every event crossed by the jump before
            // resuming normal cursor-fed scoring at the destination.
            _advanceCursorsToTime(hw, state, t);
        }

        // Collects the old phrase's tail (infinite-cutoff enqueue, final
        // forced poll, discard-then-reset) -- including committing NOTHING on
        // rewound/jumped-forward frames, in gaps, or for empty phrases. The
        // completed ledger snapshot travels with the commit, so the attempt
        // record observes the completed phrase, not the reset state.
        var transition = _advanceScorePhrase(hw, state, phrases, t, provider, {
            rewound: rewound, jumpedForward: jumpedForward,
        });
        var idx = transition.idx;
        if (transition.completed !== null) {
            commit(transition.completed);
        }
        if (idx < 0) return;

        var p = phrases[idx];
        var lookback = 0.6;   // seconds — give the scorer time to settle a judgment
        _enqueueScoreEvents(hw, state, p, t - lookback);
        _pollScorePending(state, provider, t, false);
    }

    // The note-state provider for one highway, or a falsy value (the getter's
    // own null/undefined) when no scorer is active yet. Read fresh per call --
    // the provider can appear mid-song.
    function _scoreProvider(hw) {
        return typeof hw.getNoteStateProvider === 'function' ? hw.getNoteStateProvider() : null;
    }

    // Skip every event crossed by a forward seek before resuming normal
    // cursor-fed scoring at the destination. Reads the highway's arrays
    // once (the chart can change under a seek), advances the passed
    // state's cursors.
    function _advanceCursorsToTime(hw, state, playbackTime) {
        var notes = typeof hw.getFilteredNotes === 'function' ? hw.getFilteredNotes() : [];
        var chords = typeof hw.getFilteredChords === 'function' ? hw.getFilteredChords() : [];
        _advanceScoreCursors(state, notes, chords, playbackTime);
    }

    // ---- Split Screen adaptation -----------------------------------------
    // Split Screen creates each panel detector through the public
    // createNoteDetector({ highway, ownSource }) factory.  Observing that
    // construction is the only Ladder-side integration needed: it avoids
    // reaching into Split Screen's private panel array, while giving us the
    // exact highway that owns the note-state provider.
    var _splitScoreStates = new Map();
    var _splitPanelsUnsubscribe = null;
    var _playerContextUnsubscribers = [];
    var _untaggedSplitKeys = new WeakMap();
    var _untaggedSplitSequence = 0;
    var _masteryLifecycleUnsubscribes = [];

    function _splitRegistrationKey(hw, context) {
        var scoped = playerContextKey(context);
        if (scoped) return scoped;
        if (!hw || (typeof hw !== 'object' && typeof hw !== 'function')) return null;
        var fallback = _untaggedSplitKeys.get(hw);
        if (!fallback) {
            fallback = 'untagged-split::panel-' + (++_untaggedSplitSequence);
            _untaggedSplitKeys.set(hw, fallback);
        }
        return fallback;
    }

    function _resetSplitScoreState(state, context, stableKey) {
        state.judgedKeys = new Set();
        state.pendingJudgments = new Map();
        state.phraseHits = 0;
        state.phraseTotal = 0;
        state.phraseJudgments = [];
        state.phrasesScored = 0;
        state.curPhraseIdx = -1;
        state.lastScoredT = -1;
        state.lastScoredWallT = -1;
        state.noteCursor = 0;
        state.chordCursor = 0;
        state.emaHitRate = null;
        state.lastObservedMasteryPct = null;
        // Diagnostics-only on the main path (`contributeDiagnostics` reads it
        // off `_mainScore`); carried on split states too so the shared commit
        // step and the field-parity test treat both initializers alike. Never
        // populated on split -- panels contribute no diagnostics payload.
        state.lastAutoAction = null;
        state.rampDirection = null;
        state.rampProgress = 0;
        state.downStreak = 0;
        state.manualOverride = false;
        state.context = normalizePlayerContext(context);
        state.playerKey = stableKey || playerContextKey(context);
        return state;
    }

    function newSplitScoreState(context) {
        return _resetSplitScoreState({}, context, playerContextKey(context));
    }

    function registerSplitHighway(hw, context) {
        if (!hw) return;
        var normalized = normalizePlayerContext(context);
        // Older Split Screen builds supplied only a highway. Give each such
        // pane an in-memory identity so scorer/manual-override state cannot
        // collide, but keep context null so untagged panes cannot persist.
        var stableKey = _splitRegistrationKey(hw, normalized || context);
        if (stableKey) {
            _splitScoreStates.forEach(function (state, registeredHighway) {
                if (registeredHighway !== hw && state.playerKey === stableKey) {
                    _splitScoreStates.delete(registeredHighway);
                    _cancelSectionDifficultiesForPlayer(stableKey);
                }
            });
        }
        if (_splitScoreStates.has(hw)) {
            var existing = _splitScoreStates.get(hw);
            var playerChanged = !!(stableKey && existing.playerKey && existing.playerKey !== stableKey);
            if (normalized && (playerChanged
                || persistenceContextKey(existing.context) !== persistenceContextKey(normalized))) {
                if (existing.playerKey) _cancelSectionDifficultiesForPlayer(existing.playerKey);
                _resetSplitScoreState(existing, normalized, stableKey || existing.playerKey);
                _restoreOrScheduleSections(normalized, hw);
            } else {
                if (normalized) existing.context = normalized;
                if (stableKey) existing.playerKey = stableKey;
            }
            return;
        }
        var state = newSplitScoreState(context);
        if (stableKey) state.playerKey = stableKey;
        _splitScoreStates.set(hw, state);
        if (normalized) _restoreOrScheduleSections(normalized, hw);
        startRafLoops();
    }

    function installSplitScreenDetectorHook() {
        var factory = window.createNoteDetector;
        if (typeof factory !== 'function' || factory.__ddSplitWrapped) return;
        function wrapped(options) {
            var detector = factory.apply(this, arguments);
            var ss = window.feedBackSplitscreen || window.slopsmithSplitscreen;
            if (options && options.ownSource === true && options.highway
                && ss && typeof ss.isActive === 'function' && ss.isActive()) {
                registerSplitHighway(
                    options.highway,
                    options.player_context || options.playerContext || options.context || options
                );
                // Split Screen destroys and recreates detectors when a panel
                // changes arrangement.  Release its isolated score state at
                // the same lifecycle edge so obsolete highways cannot keep
                // a rAF-side reference alive.
                if (detector && typeof detector.destroy === 'function' && !detector.destroy.__ddSplitWrapped) {
                    var destroy = detector.destroy;
                    function wrappedDestroy() {
                        _splitScoreStates.delete(options.highway);
                        return destroy.apply(this, arguments);
                    }
                    wrappedDestroy.__ddSplitWrapped = true;
                    detector.destroy = wrappedDestroy;
                }
            }
            return detector;
        }
        wrapped.__ddSplitWrapped = true;
        window.createNoteDetector = wrapped;
    }

    function startSplitScreenHookSubscription() {
        var fb = window.feedBack;
        if (_splitPanelsUnsubscribe || !fb || typeof fb.on !== 'function') return;
        var handler = function () {
            // The single-player HUD is hidden throughout Split Screen. Clear
            // its streak at either panel transition so it cannot resume with
            // a count that predates an unrelated multiplayer session.
            resetMasteryStreak();
            installSplitScreenDetectorHook();
        };
        var unsubscribe = fb.on('splitscreen:panels-changed', handler);
        _splitPanelsUnsubscribe = typeof unsubscribe === 'function'
            ? unsubscribe
            : (typeof fb.off === 'function' ? function () { fb.off('splitscreen:panels-changed', handler); } : function () {});
    }

    function stopSplitScreenHookSubscription() {
        if (!_splitPanelsUnsubscribe) return;
        _splitPanelsUnsubscribe();
        _splitPanelsUnsubscribe = null;
    }

    function startPlayerContextSubscriptions() {
        var fb = window.feedBack;
        if (_playerContextUnsubscribers.length || !fb || typeof fb.on !== 'function') return;
        function subscribe(name, handler) {
            var unsubscribe = fb.on(name, handler);
            _playerContextUnsubscribers.push(typeof unsubscribe === 'function'
                ? unsubscribe
                : (typeof fb.off === 'function' ? function () { fb.off(name, handler); } : function () {}));
        }
        function upsertEvent(ev) { upsertPlayerContext(ev && ev.detail); }
        function removeEvent(ev) { removePlayerContext(ev && ev.detail); }
        subscribe('player-context:ready', upsertEvent);
        subscribe('player-context:changed', upsertEvent);
        subscribe('player-context:left', removeEvent);
    }

    function stopPlayerContextSubscriptions() {
        _playerContextUnsubscribers.splice(0).forEach(function (unsubscribe) { unsubscribe(); });
    }

    function startMasteryLifecycleSubscriptions() {
        var fb = window.feedBack;
        if (_masteryLifecycleUnsubscribes.length || !fb || typeof fb.on !== 'function') return;
        ['song:pause', 'song:stop', 'song:ended'].forEach(function (eventName) {
            var unsubscribe = fb.on(eventName, resetMasteryStreak);
            _masteryLifecycleUnsubscribes.push(typeof unsubscribe === 'function'
                ? unsubscribe
                : (typeof fb.off === 'function'
                    ? function () { fb.off(eventName, resetMasteryStreak); }
                    : function () {}));
        });
    }

    function stopMasteryLifecycleSubscriptions() {
        _masteryLifecycleUnsubscribes.splice(0).forEach(function (unsubscribe) { unsubscribe(); });
    }

    // Split-player hooks for the shared commit step (#164): the per-panel
    // side channels. Split records a phrase attempt only when the panel has
    // a player context -- an untagged (legacy) panel logs none -- while the
    // main path always records one; that scope difference is the caller's
    // property, not the shared machine's.
    function _splitCommitHooks(state) {
        return {
            get contextForAttempt() { return state.context; },
            stateForAttempt: state,
            highwayForAttempt: null,
            recordStreak: false,
            clearRampDirectionOnEarlyReturn: false,
            onManualOverride: function (panelState) {
                // This compatibility path has no change-origin metadata.
                // Treat drift conservatively as a possible manual override
                // and disable only this controller; it is not proof a person
                // moved the slider.
                panelState.manualOverride = true;
            },
            applyStep: function (panelState, hw, curPct, step, direction, next) {
                if (next !== curPct && typeof hw.setMastery === 'function') {
                    if (panelState.context) _applyDifficultyForContext(panelState.context, next, hw, 'adaptive');
                    else hw.setMastery(next / 100); // legacy Split Screen: isolated but intentionally not persisted
                    panelState.lastObservedMasteryPct = next;
                    panelState.rampProgress = (panelState.rampProgress + 1) % RAMP_PHRASES;
                }
            },
            afterCommit: null,
        };
    }

    function commitSplitPhraseResult(state, hw, ratio) {
        _commitScoreRatio(state, ratio, hw, _splitCommitHooks(state));
    }

    function tickOneSplitHighway(hw, state) {
        if (!hw || typeof hw.hasPhraseData !== 'function' || !hw.hasPhraseData()) return;
        // One parameterized pass over this panel's highway (#165): the same
        // `_tickScoreHighway` the main player runs, with the panel's state
        // and a commit bound to it. State stays isolated per panel -- the
        // shared steps only touch the state object they are handed.
        _tickScoreHighway(hw, state, function (completed) {
            commitSplitPhraseResult(state, hw, completed);
        });
    }

    function tickSplitScoring() {
        _splitScoreStates.forEach(function (state, hw) { tickOneSplitHighway(hw, state); });
    }

    function _splitScoreStateForHighway(hw) {
        return _splitScoreStates.get(hw);
    }

    function _resetSplitManualOverrideForContext(context) {
        var key = playerContextKey(context);
        if (!key) return;
        _splitScoreStates.forEach(function (state) {
            if (state.playerKey !== key) return;
            state.manualOverride = false;
            state.lastObservedMasteryPct = null;
        });
    }

    // ---- Glass-filling HUD (overlay contract: own canvas, own rAF) ----
    var _hudCanvas = null;
    var _hudRafHandle = null;
    var _playerEl = null;   // cached — re-resolved only if disconnected, never per-frame-queried
    var GLASS_W = 26, GLASS_GAP = 8, GLASS_MAX_H = 44, GLASS_MIN_H = 16, LOOKAHEAD = 5;
    // Cached "which phrase is current" index for drawHud's own rAF loop,
    // mirroring tickScoring's _curPhraseIdx/_noteCursor cursor pattern
    // (CLAUDE.md: never redo O(N) work on a per-frame path). drawHud runs
    // independently of tickScoring (it must keep drawing even when no
    // scorer/provider is active), so it needs its own cached index rather
    // than reusing _curPhraseIdx, which tickScoring only maintains while a
    // provider is present. Reset on song change alongside the rest of
    // resetPerSongState()'s per-song cursors.
    var _hudPhraseIdx = -1;

    function getPlayerEl() {
        if (!_playerEl || !_playerEl.isConnected) _playerEl = document.getElementById('player');
        return _playerEl;
    }

    function ensureHudCanvas() {
        if (_hudCanvas && _hudCanvas.isConnected) return _hudCanvas;
        var player = getPlayerEl();
        if (!player) return null;
        _hudCanvas = document.createElement('canvas');
        _hudCanvas.id = 'dynamic-difficulty-hud';
        _hudCanvas.style.cssText =
            'position:absolute;top:8px;left:50%;transform:translateX(-50%);' +
            'pointer-events:none;z-index:15;';
        player.appendChild(_hudCanvas);
        return _hudCanvas;
    }

    function isPlayerActive() {
        var player = getPlayerEl();
        return !!(player && player.classList.contains('active'));
    }

    function drawHud() {
        if (!isPlayerActive()) {
            if (_hudCanvas) _hudCanvas.style.display = 'none';
            _hudRafHandle = null;
            return;
        }
        _hudRafHandle = requestAnimationFrame(drawHud);

        // Section Map renders the same difficulty glasses in its section bar.
        // Its idempotency marker is a stable capability signal, so this check
        // does not query or mutate DOM on the animation path.
        var sectionMapOwnsGlasses = !!window.__slopsmithSectionMapHooksInstalled;
        var ss = window.feedBackSplitscreen || window.slopsmithSplitscreen;
        if (!settings.showDifficultyGuide || sectionMapOwnsGlasses || (ss && typeof ss.isActive === 'function' && ss.isActive())) {
            if (_hudCanvas) _hudCanvas.style.display = 'none';
            return;
        }
        var hw = window.highway;
        if (!hw || typeof hw.hasPhraseData !== 'function' || !hw.hasPhraseData()) {
            if (_hudCanvas) _hudCanvas.style.display = 'none';
            return;
        }
        var canvas = ensureHudCanvas();
        if (!canvas) return;

        var phrases = hw.getPhrases();
        if (!phrases || phrases.length === 0) { canvas.style.display = 'none'; return; }
        canvas.style.display = '';

        var t = hw.getTime();
        // Reuse the cached index across frames instead of an O(phrases.length)
        // findIndex scan every rAF tick — only rescans when the cached phrase
        // no longer covers `t` (same cursor-caching idea tickScoring already
        // applies to _curPhraseIdx above).
        var curIdx = _hudPhraseIdx;
        if (curIdx < 0 || curIdx >= phrases.length || t < phrases[curIdx].start_time || t >= phrases[curIdx].end_time) {
            curIdx = phrases.findIndex(function (p) { return t >= p.start_time && t < p.end_time; });
        }
        _hudPhraseIdx = curIdx;
        if (curIdx < 0) curIdx = 0;

        var start = Math.max(0, curIdx - 1);
        var list = phrases.slice(start, start + LOOKAHEAD);
        if (_hudMaxDifficulty == null) {
            _hudMaxDifficulty = 1;
            phrases.forEach(function (phrase) {
                _hudMaxDifficulty = Math.max(_hudMaxDifficulty, _phraseTopDifficulty(phrase));
            });
        }
        var maxDiff = _hudMaxDifficulty;
        var mastery = typeof hw.getMastery === 'function' ? hw.getMastery() : 0;

        var w = Math.max(1, list.length * (GLASS_W + GLASS_GAP) - GLASS_GAP);
        // Reserve badge space before activation so the glass row does not jump.
        var h = GLASS_MAX_H + 30;
        var dpr = window.devicePixelRatio || 1;
        var wantW = Math.round(w * dpr), wantH = Math.round(h * dpr);
        if (canvas.width !== wantW || canvas.height !== wantH) {
            canvas.width = wantW;
            canvas.height = wantH;
            canvas.style.width = w + 'px';
            canvas.style.height = h + 'px';
        }
        var ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);

        if (_masteryStreak >= MASTERY_STREAK_PHRASES) {
            var badgeText = (w >= 90 ? '\u2605 Mastery ' : '\u2605 ') + _masteryStreak;
            ctx.font = '600 11px system-ui, sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            var measureKey = badgeText + '|' + w;
            if (_hudBadgeMeasureKey !== measureKey) {
                _hudBadgeMeasureKey = measureKey;
                _hudBadgeWidth = Math.min(w, Math.ceil(ctx.measureText(badgeText).width) + 14);
            }
            var badgeW = _hudBadgeWidth;
            var badgeX = (w - badgeW) / 2;
            ctx.fillStyle = 'rgba(34, 28, 8, 0.9)';
            ctx.strokeStyle = 'rgba(232, 192, 64, 0.9)';
            ctx.lineWidth = 1;
            ctx.beginPath();
            if (typeof ctx.roundRect === 'function') ctx.roundRect(badgeX, 1, badgeW, 16, 8);
            else ctx.rect(badgeX, 1, badgeW, 16);
            ctx.fill();
            ctx.stroke();
            ctx.fillStyle = '#f4d35e';
            ctx.fillText(badgeText, w / 2, 9);
        }

        list.forEach(function (p, i2) {
            var pTop = _phraseTopDifficulty(p);
            var sizeFrac = Math.max(0.3, pTop / maxDiff);
            var glassH = GLASS_MIN_H + (GLASS_MAX_H - GLASS_MIN_H) * sizeFrac;
            var fillFrac = _tierFillFrac(mastery, p.max_difficulty, pTop).fillFrac;
            var x = i2 * (GLASS_W + GLASS_GAP);
            var y = h - glassH - 4;
            var isCurrent = (start + i2) === curIdx;

            ctx.lineWidth = isCurrent ? 2 : 1;
            ctx.strokeStyle = isCurrent ? '#e8c040' : 'rgba(200,200,200,0.5)';
            ctx.beginPath();
            if (typeof ctx.roundRect === 'function') ctx.roundRect(x, y, GLASS_W, glassH, 4);
            else ctx.rect(x, y, GLASS_W, glassH);
            ctx.stroke();

            var fillH = Math.max(0, glassH * fillFrac - 1);
            if (fillH > 0) {
                ctx.fillStyle = fillFrac > 0.8 ? 'rgba(224,80,80,0.55)'
                    : fillFrac > 0.4 ? 'rgba(232,192,64,0.55)'
                        : 'rgba(64,128,224,0.55)';
                ctx.fillRect(x + 1, y + glassH - fillH, GLASS_W - 2, fillH);
            }
        });
    }

    // ---- Generate-difficulties CTA (calls routes.py's /generate) ----
    var _generateBtn = null;
    var _generating = false;   // double-submit guard — Slopsmith's editor plugin
                                // shipped without one on its Build button and a
                                // stray second click raced two concurrent jobs
    var _generateLabelTimer = null;

    function currentTargetStatus() {
        var hw = window.highway;
        if (!hw || typeof hw.getSongInfo !== 'function') {
            return { ok: false, reason: 'unavailable' };
        }
        // highway.getSongInfo() is chart metadata only.  In particular, its
        // song_info payload has no filename; the host publishes that separately
        // as feedBack.currentSong.filename.  Requiring si.filename here made
        // every real host song look unloaded, despite the player being active.
        var si = hw.getSongInfo() || {};
        var currentSong = (window.feedBack && window.feedBack.currentSong) || {};
        var filename = currentSong.filename || si.filename;
        if (!filename) return { ok: false, reason: 'unloaded' };
        // Highway's snake_case index describes the currently streamed
        // arrangement.  currentSong uses camelCase and is the fallback for
        // hosts that expose only the plugin-context object.
        var arrangementIndex = si.arrangement_index;
        if (arrangementIndex == null) arrangementIndex = currentSong.arrangementIndex;
        if (arrangementIndex == null) arrangementIndex = 0;
        // Defensive clamp at point of use (settings.generateLevels came from
        // localStorage and could be stale/out-of-range) — same convention as
        // thresholds()/emaAlpha() clamping settings.sensitivity/reactionSpeed
        // rather than trusting the stored value blindly. Parse once and default
        // only on NaN — `|| 4` would also catch a legitimately parsed 0.
        var parsedLevels = parseInt(settings.generateLevels, 10);
        var levels = Math.max(2, Math.min(8, isNaN(parsedLevels) ? 4 : parsedLevels));
        return {
            ok: true,
            target: {
                filename: filename,
                arrangement_index: arrangementIndex,
                levels: levels,
            },
        };
    }

    function currentTarget() {
        var status = currentTargetStatus();
        return status.ok ? status.target : null;
    }

    function updateGenerateButtonVisibility() {
        if (!_generateBtn) return;
        var hw = window.highway;
        var hasData = !!(hw && typeof hw.hasPhraseData === 'function' && hw.hasPhraseData());
        _generateBtn.style.display = hasData ? 'none' : '';
    }

    function _resetGenerateBtnLabel() {
        if (_generateBtn) _generateBtn.textContent = '⚙️ Generate Difficulties';
    }

    function _clearGenerateLabelTimer() {
        if (_generateLabelTimer) clearTimeout(_generateLabelTimer);
        _generateLabelTimer = null;
    }

    // Single choke point for every "⚙️ Generate Difficulties" label change —
    // all five states the button can show (idle, unavailable/no-song,
    // generating, failed, skipped) go through this, so _generateLabelTimer
    // only ever has one owner. `resetDelay` omitted/null means the label
    // sticks (no scheduled revert); `resetFn` defaults to restoring the
    // idle "⚙️ Generate Difficulties" text.
    function setGenerateLabel(text, resetDelay, resetFn) {
        if (!_generateBtn) return;
        _generateBtn.textContent = text;
        _clearGenerateLabelTimer();
        if (resetDelay != null) {
            _generateLabelTimer = setTimeout(function () {
                _generateLabelTimer = null;
                (resetFn || _resetGenerateBtnLabel)();
            }, resetDelay);
        }
    }

    function rememberGeneratedInstruments(filename, currentArrangement, data) {
        if (!filename || !data) return;
        var rows = Array.isArray(data.arrangements) ? data.arrangements : [];
        // Backward compatibility with the older single-arrangement response.
        if (!rows.length && data.instrument) {
            rows = [{ arrangement_index: currentArrangement, instrument: data.instrument }];
        }
        rows.forEach(function (row) {
            if (!row) return;
            // Validate the raw field rather than a Number() coercion of it:
            // Number(null) and Number('') are both 0, which would make a row
            // carrying no usable index clear arrangement 0 — now a delete
            // rather than a write. The backend always sends an int here.
            if (!Number.isInteger(row.arrangement_index) || row.arrangement_index < 0) return;
            var index = row.arrangement_index;
            var key = songKeyOf({ filename: filename, arrangement_index: index });
            if (row.instrument === 'drums' || row.instrument === 'unsupported') {
                // Issue #141: these rows are the backend's own verdict for
                // every arrangement of this song, so one generate pass repairs
                // the mislabeled siblings too — the song-info path only ever
                // sees the arrangement the user has open. A row with no
                // instrument at all is an unclassified skip (unknown index,
                // other skip reason) and is no evidence of ineligibility, so
                // it clears nothing.
                _clearStaleSongInstrument(key);
                return;
            }
            if (row.instrument !== 'fretted' && row.instrument !== 'keys') return;
            _rememberSongInstrument(key, row.instrument);
            if (index === currentArrangement) _songInstrument = row.instrument;
        });
    }

    async function onGenerateClick() {
        if (_generating) return; // guard: one in-flight generate at a time
        var status = currentTargetStatus();
        if (!status.ok) {
            var unavailable = status.reason === 'unavailable';
            console.warn(
                unavailable
                    ? '[difficulty_ladder] generate click ignored: highway.getSongInfo() is unavailable'
                    : '[difficulty_ladder] generate click ignored: no song loaded yet (highway.getSongInfo() returned nothing)'
            );
            setGenerateLabel(unavailable ? 'Player unavailable' : 'No song loaded', 2000);
            return;
        }
        var target = status.target;
        _generating = true;
        _generateBtn.disabled = true;
        setGenerateLabel('Generating…', null);
        try {
            var resp = await fetch('/api/plugins/' + PLUGIN_ID + '/generate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(target),
            });
            var data = null;
            try { data = await resp.json(); } catch (_) { /* noop */ }
            if (!resp.ok || !data || data.error) {
                console.warn('[difficulty_ladder] generate failed:', (data && data.error) || resp.status);
                setGenerateLabel('Generate failed', 2500);
                return;
            }
            // /generate is song-wide and returns one authoritative classifier
            // per arrangement, including already-authored ladders. Persist all
            // supported rows before any generated/skipped early return.
            rememberGeneratedInstruments(target.filename, target.arrangement_index, data);
            // /generate processes the full song.  A pack can mix guitar,
            // bass and keys arrangements; routes.py classifies each one and
            // intentionally skips drums.  Do not treat a partial skip as a
            // failure when other arrangements were generated successfully.
            if (data.generated === 0) {
                setGenerateLabel(
                    data.failed ? 'Generate failed' : 'Difficulties already exist',
                    2500, updateGenerateButtonVisibility
                );
                return;
            }
            setGenerateLabel(
                data.generated === 1 ? 'Generated 1 arrangement' : 'Generated ' + data.generated + ' arrangements',
                2500, updateGenerateButtonVisibility
            );
            // Reload the current song so the highway WS re-streams the new
            // phrase data (it was written server-side after this song's
            // websocket already sent its snapshot).
            var hw = window.highway;
            if (hw && typeof hw.reconnect === 'function') {
                hw.reconnect(target.filename, target.arrangement_index);
            }
        } catch (e) {
            console.warn('[difficulty_ladder] generate request failed:', e);
            setGenerateLabel('Generate failed', 2500);
        } finally {
            _generating = false;
            _generateBtn.disabled = false;
        }
    }

    // ---- Player-controls toggle (v3 chrome contract) ----
    var _controlsBtn = null;
    function syncControlsUI() {
        if (!_controlsBtn) return;
        _controlsBtn.classList.toggle('fb-primary', settings.autoAdjust);
        _controlsBtn.style.opacity = settings.autoAdjust ? '1' : '0.6';
        _controlsBtn.title = settings.autoAdjust
            ? 'Difficulty Ladder: auto-adjusting from your accuracy (click to pause)'
            : 'Difficulty Ladder: paused (click to resume auto-adjust)';
    }

    function mountControls() {
        if (!window.feedBack || window.feedBack.uiVersion !== 'v3') return;
        if (!window.feedBack.ui || typeof window.feedBack.ui.playerControlSlot !== 'function') return;
        var slot = window.feedBack.ui.playerControlSlot();
        if (!slot) return;
        if (_controlsBtn && slot.contains(_controlsBtn)) {
            syncControlsUI();
            updateGenerateButtonVisibility();
            return;
        }

        _controlsBtn = document.createElement('button');
        _controlsBtn.id = 'dynamic-difficulty-toggle';
        _controlsBtn.className = 'fb-text text-xs px-2 py-1 rounded hover:bg-white/10 flex items-center gap-1';
        _controlsBtn.textContent = '🥃 Auto-Difficulty';
        _controlsBtn.onclick = function () {
            settings.autoAdjust = !settings.autoAdjust;
            // A manual choice wins over a pending Split Screen scorer write.
            cancelDebouncedSettingWrite('autoAdjust');
            lsSet('autoAdjust', settings.autoAdjust);
            _mainScore.lastObservedMasteryPct = null;
            if (settings.autoAdjust) _resetSplitManualOverrideForContext(_mainPlayerContext);
            syncControlsUI();
        };
        slot.appendChild(_controlsBtn);
        syncControlsUI();

        _generateBtn = document.createElement('button');
        _generateBtn.id = 'dynamic-difficulty-generate';
        _generateBtn.className = 'fb-text text-xs px-2 py-1 rounded hover:bg-white/10 flex items-center gap-1';
        _generateBtn.textContent = '⚙️ Generate Difficulties';
        _generateBtn.title = 'Generate difficulty ladders for every non-drum arrangement in this song (sloppak songs only)';
        _generateBtn.onclick = onGenerateClick;
        slot.appendChild(_generateBtn);
        updateGenerateButtonVisibility();
    }

    // ---- Lifecycle ----
    function startRafLoops() {
        startMasteryLifecycleSubscriptions();
        if (!_scoreRafHandle) tickScoring();
        if (!_hudRafHandle) drawHud();
    }

    function onSongEvent() {
        ensureMasterySaveHook();
        var previousMainContext = _mainPlayerContext;
        var hw = window.highway;
        var si = (hw && typeof hw.getSongInfo === 'function') ? hw.getSongInfo() : null;
        var identity = Object.assign({}, si || {}, _songContextFields(si));
        identity.filename = identity.song_id;
        identity.arrangement_index = identity.arrangement_id;
        var key = songKeyOf(identity);
        if (key !== _songKey) {
            flushPhraseAttempts();
            flushProgressStore();
            _songKey = key;
            _songInstrument = null;
            resetPerSongState();
        }
        // This is synchronous on an older Host and may be asynchronous when
        // v3Profile/playerContexts exists. Writes remain gated until it wins
        // the resolution token and supplies a concrete profile identity.
        activateCompatibilityPlayerContext(si);
        if (key && si) {
            var instrument = _instrumentKind(si.arrangement_type, si.arrangement);
            if (instrument === 'fretted' || instrument === 'keys') {
                _songInstrument = instrument;
                _rememberSongInstrument(key, instrument);
            } else {
                // Issue #141: an arrangement the backend will not ladder must
                // not keep a label the pre-fix classifier wrote for it.
                _clearStaleSongInstrument(key);
            }
        }
        mountControls();
        updateGenerateButtonVisibility();
        startRafLoops();
        contributeDiagnostics();
        // Emit section difficulty data for sectionmap plugin. Drop any
        // still-pending debounced emit from the previous song first, so a
        // stray trailing-edge fire can't immediately re-emit stale data for
        // the song we just navigated away from.
        // Song navigation owns only the compatibility/main pane. Split panes
        // have independent lifecycle and trailing section refreshes that must
        // survive a main-player song event.
        cancelSectionDifficultiesEmit(previousMainContext || {
            session_id: _sessionId, player_id: 'main',
        });
        if (_mainPlayerContext) cancelSectionDifficultiesEmit(_mainPlayerContext);
        // A profile-aware Host may still be resolving v3Profile/getActive.
        // Emitting now would publish this pane with player_context: null and
        // let Section Map briefly consume unscoped state. The acceptance path
        // schedules the first scoped update once identity is ready. Older
        // Hosts resolve legacy-default synchronously and retain the immediate
        // single-player update.
        if (_mainPlayerContext || !_profileApisPresent()) {
            calculateAndEmitSectionDifficulties(_mainPlayerContext, window.highway);
        }
    }

    // Bind late (rule 21's "register into the host" pattern, applied here):
    // plugins load alphabetically and window.feedBack.libraryCardActions may
    // not exist the instant this script runs, so try now and again on the
    // next couple of lifecycle events that fire regardless of whether the
    // user ever opens the player — registerLibraryCardBadge() is idempotent,
    // so extra calls after the first success are free no-ops.
    registerLibraryCardBadge();

    if (window.feedBack && typeof window.feedBack.on === 'function') {
        window.feedBack.on('song:ready', onSongEvent);
        // Split Screen emits this after its panel highways are created and
        // again after a canvas/highway replacement.  By then Note Detect is
        // normally loaded; wrapping its public factory lets Ladder observe
        // future per-panel Detect clicks without depending on Split Screen
        // internals.
        startSplitScreenHookSubscription();
        startPlayerContextSubscriptions();
        window.feedBack.on('library:changed', registerLibraryCardBadge);
        window.feedBack.on('highway:created', mountControls);
        window.feedBack.on('highway:visibility', function (ev) {
            var detail = ev && ev.detail;
            if (detail && detail.visible) {
                startSplitScreenHookSubscription();
                startPlayerContextSubscriptions();
                installSplitScreenDetectorHook();
                startRafLoops();
            } else {
                resetMasteryStreak();
                flushPhraseAttempts();
                flushProgressStore();
                stopMasteryLifecycleSubscriptions();
                stopSplitScreenHookSubscription();
                stopPlayerContextSubscriptions();
                if (_scoreRafHandle) { cancelAnimationFrame(_scoreRafHandle); _scoreRafHandle = null; }
                if (_hudRafHandle) { cancelAnimationFrame(_hudRafHandle); _hudRafHandle = null; }
                _clearGenerateLabelTimer();
                cancelSectionDifficultiesEmit();
                if (_hudCanvas) _hudCanvas.style.display = 'none';
                if (_generateLabelTimer) { clearTimeout(_generateLabelTimer); _generateLabelTimer = null; }
            }
        });
    }

    // Safety net: if highway:visibility is not fired for every player-active
    // transition (e.g. pause/resume without a song change), the document
    // visibilitychange event ensures we restart both loops whenever the tab
    // returns to the foreground while the player is active.
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') startRafLoops();
        else resetMasteryStreak();
    });
    // Core rebuilds the Profile shell with innerHTML on every entry, so this
    // event is the stable extension seam and must remain subscribed while the
    // player itself is hidden.
    document.addEventListener('v3:profile-rendered', renderProfileBaseline);

    // The reaction to a changed setting is the same on both notification paths:
    // a foreign tab's `storage` write names one key, this tab's
    // `difficulty_ladder:settings-changed` carries a patch, and both have already
    // written the raw values into `settings` by the time we get here. `changed`
    // is therefore only the set of names to react to, and it is read as own
    // properties rather than truthiness — a setting switched to `false` or `0` is
    // a change like any other.
    function _applySettingsChange(changed) {
        var has = function (name) { return Object.prototype.hasOwnProperty.call(changed, name); };
        if (has('autoAdjust') && settings.autoAdjust === true) _resetSplitManualOverrideForContext(_mainPlayerContext);
        if (has('dropResistance')) {
            settings.dropResistance = settings.dropResistance === true;
            _mainScore.downStreak = 0;
        }
        if (has('levelUpOnly')) {
            settings.levelUpOnly = settings.levelUpOnly === true;
            _mainScore.rampDirection = null;
            _mainScore.rampProgress = 0;
        }
        if (has('minMastery') || has('maxMastery')) _normalizeMasteryBounds();
    }

    // Settings panel writes localStorage directly (see settings.html) and
    // notifies us to re-read rather than us polling localStorage per frame.
    window.addEventListener('storage', function (e) {
        if (!e.key || e.key.indexOf(LS_PREFIX) !== 0) return;
        // A foreign tab just wrote one of our keys. Same race for both
        // debounced v2 stores: don't discard our own pending write, and drop
        // the cache so the next read picks up the merged-by-last-write-wins
        // reality.
        if (e.key === PROGRESS_LS_KEY) _progressStore.invalidate();
        if (e.key === PHRASE_ATTEMPTS_V2_LS_KEY) _phraseAttemptStore.invalidate();
        if (e.key === SONG_MASTERY_LS_KEY) _songMasteryStore.invalidate(false);
        var short = e.key.slice(LS_PREFIX.length);
        // Issue #158: a foreign tab running a pre-rename build may still
        // write the legacy key. Treat it as the new setting (persisted
        // forward), so a pre-upgrade tab can't silently lose the user's
        // choice.
        if (short === 'showGlasses') {
            try { settings.showDifficultyGuide = JSON.parse(e.newValue) === true; } catch (_) {
                settings.showDifficultyGuide = false;
            }
            lsSet('showDifficultyGuide', settings.showDifficultyGuide);
            syncControlsUI();
            contributeDiagnostics();
            return;
        }
        if (Object.prototype.hasOwnProperty.call(settings, short)) {
            try { settings[short] = JSON.parse(e.newValue); } catch (_) {
                if (short === 'dropResistance' || short === 'levelUpOnly') settings[short] = false;
            }
            var changed = {};
            changed[short] = true;
            _applySettingsChange(changed);
            syncControlsUI();
            contributeDiagnostics();
        }
    });
    window.addEventListener(PLUGIN_ID + ':settings-changed', function (ev) {
        var patch = ev && ev.detail;
        if (!patch) return;
        Object.assign(settings, patch);
        _applySettingsChange(patch);
        syncControlsUI();
        contributeDiagnostics();
    });

    // Node-only export hook for tests (mirrors the convention used by
    // feedBack-plugin-sectionmap's screen.js): expose the pure/DOM-light
    // helpers so they're unit-testable without a browser, and skip the
    // side-effect wiring below (event-bus subscriptions, rAF loops) since
    // there's no real window.feedBack/highway to wire up against in Node.
    // Browsers never hit this branch (`module` is undefined), so runtime
    // behavior is unchanged.
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            thresholds, emaAlpha, downStepRatio, songKeyOf,
            judgmentKey, settings,
            _resolveDifficultyGuideSetting,
            _normalizeMasteryBounds,
            _dominantSongMastery,
            aggregateMasteryByInstrument, renderProfileBaseline,
            _masteryPct, _rememberSongInstrument, _instrumentKind,
            loadSongMasteryMap, saveSongMasteryMap,
            normalizePlayerContext, playerContextKey, persistenceContextKey,
            _nodeKey,
            loadProgressStore, saveProgressStore, readProgress, writeProgress,
            flushProgressStore,
            migrateLegacyData, resolveCompatibilityPlayerContext,
            activateCompatibilityPlayerContext,
            upsertPlayerContext, removePlayerContext, listPlayerContexts,
            loadPhraseAttemptStore, loadPhraseAttempts, savePhraseAttempts,
            recordPhraseAttempt, _phraseIdOf,
            _presentedDifficultyLevel, _tierFillFrac, _phraseTopDifficulty, _chartHasContentIn,
            _sectionsV3Payload, _v3TierRange, _v3CurrentTier, _v3PhraseSongKey,
            calculateAndEmitSectionDifficulties,
            commitPhraseResult, resetPerSongState,
            updateMasteryStreak, resetMasteryStreak, masteryStreakStatus,
            startMasteryLifecycleSubscriptions, stopMasteryLifecycleSubscriptions,
            MASTERY_STREAK_PHRASES, MASTERY_STREAK_ACCURACY,
            rampStep, rampDirection, WARMUP_PHRASES, RAMP_PHRASES,
            RESUME_OFFSET_FRACTION,
            _maybeRestoreSongMastery, _resumeStartPct,
            currentTarget, currentTargetStatus,
            mountControls, onGenerateClick, rememberGeneratedInstruments, onSongEvent,
            newSplitScoreState: newSplitScoreState, commitSplitPhraseResult: commitSplitPhraseResult,
            registerSplitHighway, tickOneSplitHighway: tickOneSplitHighway,
            _splitScoreStateForHighway,
            // Shared-machine surface (#164/#165): the main player's default
            // state plus the shared steps both pipelines run. Exported for
            // tests.
            _mainScore: function () { return _mainScore; },
            _resetMainScoreState,
            _enqueueScoreEvents, _pollScorePending,
            _advanceScoreCursors, _advanceCursorsToTime,
            _updateScoreDiscontinuity, _advanceScorePhrase,
            _tickScoreHighway, _scoreProvider,
            _commitScoreRatio, _mainCommitHooks, _splitCommitHooks,
            _applyDifficultyForContext,
            _contextEventPayload,
            _onMasteryApplied,
            _isForwardScoringDiscontinuity,
         };
        return;
    }

    ensureMasterySaveHook();
    installSplitScreenDetectorHook();
    startPlayerContextSubscriptions();
    startRafLoops();
})();
