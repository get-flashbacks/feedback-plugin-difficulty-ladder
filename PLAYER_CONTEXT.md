# Player Context Contract v1

This document defines the context that keeps Difficulty Ladder state isolated
when several people play at once. It is the contract for the Host,
Split Screen, `note_detect`, karaoke, and Section Map integrations.

## Context shape

Every concurrent player is identified by a stable `(session_id, player_id)`
pair. A ready context has this shape:

```json
{
  "schema": "difficulty_ladder.player_context.v1",
  "session_id": "split-session-42",
  "player_id": "player-2",
  "profile_id": "profile-alex",
  "profile_hash": "profile-hash-alex",
  "song_id": "library/song.feedpak",
  "arrangement_id": "lead",
  "instrument": "guitar",
  "role": "lead",
  "skill": "overall"
}
```

`profile_id` or `profile_hash` must identify the profile. `highway` may be
attached to an in-memory context for a panel, but is never serialized in an
event payload. `skill` defaults to `overall`; future skills such as
`pinch-harmonics`, `bends`, or `vocal-pitch` must have independent records.

Persistence is nested by these dimensions:

```text
profile → player → song → arrangement → instrument → role → skill
```

`player_id` is deliberately persisted below the profile. Two simultaneous
players who select the same profile still own separate progress and phrase
attempt records.

`currentDifficulty` (the live target) and `bestMastery` (the long-term best)
are different values. A skill-specific record never overwrites `overall`.
At each finalized phrase, mastery is calculated as
`live difficulty percentage × judged hit rate`; `bestMastery` stores the
monotonic maximum of those results.
Reading a missing skill may fall back to `overall`, but an explicit instrument
or role must never read another instrument's scoped record. The sole exception
is an idempotently migrated v1 record that had no instrument: it is explicitly
marked unscoped and may seed the claiming profile/player's matching song and
arrangement once, without crossing a profile or player boundary.

Karaoke contexts use `role: "karaoke"` and `instrument: "voice"`. They are
stored independently from guitar, bass, keys, and other instrumental roles.

## Events and capabilities

The plugin advertises the context contract at:

```js
window._ddCapabilities.playerContext ===
  'difficulty_ladder.player_context.v1'
```

The Host event bus carries these events. Their `detail` is a context object
with the shape above:

| Event | Meaning |
|---|---|
| `player-context:ready` | A player has a resolved profile and playable context. |
| `player-context:changed` | Profile, song, arrangement, instrument, role, or skill changed. |
| `player-context:left` | The player left the session; consumers release panel state. |

Adaptive changes use the player-scoped capability dispatch pipeline:

```json
{
  "schema": "difficulty_ladder.difficulty_request.v1",
  "action": "set",
  "player_context": {
    "schema": "difficulty_ladder.player_context.v1",
    "session_id": "split-session-42",
    "player_id": "player-2",
    "profile_id": "profile-alex",
    "profile_hash": "profile-hash-alex",
    "song_id": "library/song.feedpak",
    "arrangement_id": "lead",
    "instrument": "guitar",
    "role": "lead",
    "skill": "overall"
  },
  "current_difficulty": 67,
  "reason": "adaptive"
}
```

The complete `player_context` is included in real payloads. The request must
target only that player's highway/arrangement. Difficulty Ladder treats only a
literal `true` dispatcher result as acceptance; any other result falls through
to the context-owned highway or the safe single-player compatibility setter.

After a change, Difficulty Ladder emits `difficulty:player-changed`:

```json
{
  "schema": "difficulty_ladder.difficulty_event.v1",
  "player_context": {
    "schema": "difficulty_ladder.player_context.v1",
    "session_id": "split-session-42",
    "player_id": "player-2",
    "profile_id": "profile-alex",
    "profile_hash": "profile-hash-alex",
    "song_id": "library/song.feedpak",
    "arrangement_id": "lead",
    "instrument": "guitar",
    "role": "lead",
    "skill": "overall"
  },
  "current_difficulty": 67,
  "reason": "adaptive"
}
```

`difficulty:sections-updated` uses the payload schema
`difficulty_ladder.sections.v2` and also carries `player_context` when it is
generated for a concurrent panel. Section Map must use that context to update
only the matching pane. A legacy main-player emission may have a null context
while an older Host is still loading identity.

## Readiness and lifecycle

Profile identity is asynchronous on profile-aware Hosts. Consumers must not
read or write profile-scoped progress until the identity is ready. During the
pending state, Difficulty Ladder rejects persistence writes and does not claim
legacy data. A profile API that is present but unresolved must not silently
fall back to a shared default profile.

Profile API exceptions keep the main adapter gated and emit
`difficulty:profile-context-error` (`difficulty_ladder.profile_context_error.v1`).
The next song/profile lifecycle activation retries resolution; the
`legacy-default` profile is used only when no profile API exists.

The legacy adapter is allowed only when no profile-context API exists and the
Host is operating as one main player. It uses `profile_id: "legacy-default"`
and retains the old storage keys as recovery sources. Concurrent contexts must
never claim that unscoped legacy data.

Player contexts are created or updated on `ready`/`changed`, and removed on
`left`. Song, arrangement, role, or skill changes reset transient scoring state
for that player while leaving persisted records for other contexts untouched.
For older lifecycle producers, a `left` payload that omits `session_id` is
matched against the same current-session default used by `ready`/`changed`.
Pause, seek, loop, detector replacement, and highway replacement must release
or reset transient state without changing another player's controller.

## Note detection and finalization

`note_detect` owns note judgment. Its provider is attached to the player's
highway and returns only the established `hit`, `active`, or `miss` states.
Difficulty Ladder samples that provider on the matching highway and counts a
note once, ignoring unresolved `active` results until they resolve.

The current implementation finalizes a phrase when playback crosses into the
next phrase, then records a player-scoped `difficulty_ladder.phrase_attempt.v2`
record. It does not score from a global detector or a global active profile.
If `note_detect` exposes a separate finalized-session event in the future, it
must include the same `session_id`, `player_id`, profile, arrangement, and
instrument/role/skill dimensions; consumers must deduplicate it against the
phrase/session already finalized by the highway.

`note_detect` must not call an unscoped `window.setMastery` for a split player.
Difficulty changes are requested through `player-difficulty.v1` with the
player context, so the Host or Split Screen can route them to the correct
highway.

## Responsibilities by integration

### Host

- Provide the event bus and capability dispatch/claim/release pipeline.
- Provide a profile identity before exposing a ready context.
- Keep context and difficulty operations player-scoped; a global active profile
  is insufficient for concurrent play.
- Preserve graceful feature detection for older single-player Hosts.

### Split Screen

- Support at least four simultaneous contexts (`player-1` through `player-4`)
  in one session, with stable IDs for the lifetime of each panel.
- Select and publish each player's profile, instrument/role, arrangement, and
  optional skill independently.
- Pass the matching highway and context to the public detector factory, using
  `ownSource: true`, and emit panel/detector replacement lifecycle events.
- Route player-scoped difficulty requests and section updates to one pane.
- Older untagged panel registrations receive unique per-highway in-memory
  controller identities so manual overrides cannot collide. They remain
  persistence-gated until an explicit ready player context is supplied.

### `note_detect`

- Register one note-state provider per player highway.
- Include player context in any session/finalization event.
- Keep judgment ownership and finalization idempotent; never cross-feed panels.

### Karaoke

- Publish vocal players as `role: "karaoke"`, `instrument: "voice"` (or a
  compatible role that normalizes to those values), with the player's profile
  and arrangement IDs.
- Keep vocal pitch, lyric timing, harmony, and future vocal skills under their
  own skill keys. Do not reuse fretted-instrument records.

### Section Map

- Consume `difficulty:sections-updated` by `session_id` and `player_id` when
  present, rendering glasses only in the matching player pane.
- Preserve the existing no-data behavior: absent or delayed phrase data means
  no misleading zero/fully-filled glass.

## Compatibility boundary

The old `window.setMastery` and unscoped main-player behavior remain adapters
for older Hosts. They are not the concurrency contract. New integrations must
use context-carrying events and `player-difficulty.v1`; adding a fourth player,
changing a profile, or adding a new skill must not overwrite another context's
state.
