# FeedBack Dynamic Difficulty vs. Rocksmith's Dynamic Difficulty Engine

## Executive Summary

Our **Dynamic Difficulty plugin** shares Rocksmith's core philosophy—adapting song difficulty to player skill via phrase-level analysis—but diverges significantly in scope, architecture, and real-time responsiveness. Rocksmith's engine is a **commercially-built, closed-source real-time system** tuned by Ubisoft across millions of players; ours is a **lightweight, heuristic-driven practice tool** optimized for accuracy observation and sloppak/feedpak charts.

---

## Side-by-Side Architectural Comparison

| Metric | Our Plugin | Rocksmith (2014+) |
|---|---|---|
| **Difficulty levels per phrase** | 2–8 (configurable; default 4) | 2–31 (author-defined per song) |
| **Adjustment timing** | Section boundaries only | Real-time, per-note or rapid rolling window |
| **Adjustment trigger** | Phrase-end accuracy ratio vs. EMA thresholds | Real-time pitch/timing confidence (millisecond-grained) |
| **Adjustment granularity** | Whole-number % steps (5, 10, 15, 20%) | Unknown; likely sub-1% or continuous curve |
| **Master/overgain mode** | Not implemented | Yes (110% difficulty, fading notes) |
| **Fixed difficulty tiers** | Not implemented | Score Attack: 4 fixed (Easy/Medium/Hard/Master) |
| **Per-song memory** | Yes (`localStorage`, per filename+arrangement) | Yes (built into game save, per-song progression) |
| **Manual override doctrine** | Auto-adjust disables on slider move | Game pauses difficulty changes during manual input |
| **Note-state consumer** | Reads from active scorer via `getNoteStateProvider()` | Proprietary scorer; multi-source (pitch, timing, velocity) |
| **Audio I/O model** | Passive observer; host provides judgments | Real-time DSP pipeline (pitch detection, timing analysis) |

---

## Feature Breakdown

### 1. Difficulty Ladder Generation

**Our approach (routes.py):**
- Analyzes note/chord **density, fret complexity, technique, span, sustain** per group
- Scores each group on a 0–1 scale
- Percentile-buckets groups into tiers (Easy/Medium/Hard/etc.)
- Thins lower tiers by removing notes/voicings; top tier stays byte-identical to source
- **Deterministic & offline** — runs once per arrangement, writes to disk
- **Instrument-aware:** Fretted (guitar/bass) vs. Keys vs. Drums (skipped)

**Rocksmith's approach (reverse-engineered):**
- Evaluates author-assigned difficulty per phrase (not auto-generated)
- Each phrase carries **2–31 discrete levels**, manually authored or toolkit-assisted
- Levels represent incremental note-density/complexity thinning (similar philosophy to ours)
- Real-time adjustment selects which level to play, not generating new levels
- **Chart author owns the difficulty ladder quality** — Ubisoft doesn't auto-generate

**Verdict:** Our generation is a **best-effort fill-in** for charts that lack phrase data (e.g., GP imports). Rocksmith assumes hand-tuned data from the start.

---

### 2. Real-Time Difficulty Adjustment

**Our approach (screen.js):**
```js
// Per-phrase scoring loop (tickScoring):
1. Collects hit/miss judgments from note_detect scorer for last 2 seconds
2. Waits until phrase END to commit: hitRate = _phraseHits / _phraseTotal
3. Updates EMA: emaHitRate = α * hitRate + (1 - α) * oldEMA
   - α = 0.20 (slow) to 0.50 (fast), default 0.35
4. At section boundary, checks EMA against thresholds:
   - If EMA ≥ 0.88–0.93 (sensitivity-dependent) → nudge up by 5–20% steps
   - If EMA ≤ 0.65–0.71 (sensitivity-dependent) → nudge down by 5–20% steps
5. Calls window.setMastery(newPct) ONCE per section
```

**Rocksmith's approach:**
```
1. Real-time pitch/timing confidence: per note or rolling 100–500ms window
2. Immediate scoring: hit / active (sustain held correctly) / miss
3. Confidence threshold evaluation: likely sub-100ms latency
4. Adaptive algorithm (proprietary):
   - Possibly curve-based (smooth ramping) rather than step-based
   - Likely per-note or per-phrase window, not per-phrase-end
   - Likely includes velocity, pitch-bend stability, timing jitter
5. Visual/game-state changes: immediate (next note may be higher/lower)
```

**Verdict:** Rocksmith reacts **in real-time, within play latency** (~50–200ms window). Our plugin waits for **phrase boundaries** (~5–30 seconds), trading responsiveness for stability. Rocksmith's unknown algorithm likely uses **confidence scoring** rather than binary hit/miss.

---

### 3. Relative vs. Total Difficulty

**Our plugin (not explicitly modeled):**
- `master_difficulty` slider: **0–100%, represents current tier** (e.g., "Medium" = 50% of max_difficulty)
- Glass HUD: **fill level = current tier / phrase's max tier**
- No distinction between "what you're playing now" and "the hardest version exists"
- For a 4-level arrangement at 50% mastery, you see a pre-thinned note set (not the full transcription)

**Rocksmith's model:**
- **Relative Difficulty**: Menu sorting metric; "relative to this song's complexity or your mastery"
- **Total Difficulty (100%)**: Full, unedited artist transcription; all notes/embellishments/solos present
- Master Mode: Play 100% with notes fading → memory test

**Verdict:** Rocksmith explicitly separates **"current view"** (relative, filtered by mastery slider) from **"complete chart"** (total, 100% difficulty). Our plugin blurs this; we thin lower tiers but don't preserve a "full transcription at 100%" separate from the phrase ladder we generate.

---

### 4. Accuracy Evaluation & Confidence

**Our approach:**
- Binary per-note: `hit` or `miss` (from note_detect provider)
- Phrase-level ratio: `hits / total` → accuracy %
- Threshold crossing triggers step change

**Rocksmith:**
- Multi-dimensional: pitch accuracy, timing accuracy, velocity
- Real-time confidence curves (not disclosed)
- Likely uses **running correlation** or **likelihood scoring** for sustain holds
- State machine: `hit` (correct) → `active` (sustain phase) → `miss` (drop/timeout)

**Verdict:** Rocksmith's scoring is **continuous** (confidence 0–1 per note). Ours is **discrete** (hit or miss). This explains why Rocksmith can adjust **continuously/smoothly** while we step in 5–20% increments.

---

### 5. Settings & User Control

**Our plugin (settings.html):**
| Setting | Effect | Rocksmith equivalent |
|---------|--------|----------------------|
| Auto-adjust difficulty | Master toggle | Option in main menu |
| Glass HUD | Show/hide only | N/A (always visible) |
| Sensitivity (1–3) | Thresholds: 0.93→0.83, 0.65→0.71 | Difficulty sensitivity (if exposed) |
| Reaction speed (1–3) | EMA α: 0.20–0.50 | Response speed / skill growth rate |
| Min/Max % | Hard bounds | Auto-difficulty floor/ceiling (if exposed) |

**Rocksmith's known controls:**
- Dynamic Difficulty: On/Off toggle (Learn a Song mode only)
- (Score Attack: fixed tiers, no adjustment)
- Auto-leveling: On/Off
- Difficulty sensitivity: (proprietary, not user-exposed as a slider)

**Verdict:** We offer **more granular user control** (3-axis tuning). Rocksmith prioritizes **simplicity** (binary on/off).

---

## Limitations & Gaps

### What We Do Well
✅ **Lightweight observation model** — doesn't require proprietary audio DSP; works with any note-detection scorer  
✅ **Configurable sensitivity** — 3 independent axes (thresholds, reaction speed, bounds)  
✅ **Per-song memory** — restores difficulty when returning to a song  
✅ **Glass HUD** — clear visual of upcoming sections  
✅ **Instrument coverage** — fretted, keys, drums-aware skipping  
✅ **Regeneration** — fills gaps in charts lacking phrase data  

### Where Rocksmith Wins
❌ **Real-time responsiveness** — We wait for phrase boundaries; Rocksmith adjusts mid-phrase  
❌ **Smooth adjustment curves** — We step 5–20%; Rocksmith likely curves smoothly  
❌ **Master Mode** — No 110% overgain / memory test mode  
❌ **Fixed tiers** — No Score Attack equivalent (strict, no auto-adjust)  
❌ **Precision scoring** — Binary hit/miss vs. Rocksmith's confidence dimensions (pitch, timing, velocity)  
❌ **Author intent preservation** — We generate from scratch; Rocksmith trusts hand-authored tiers  
❌ **Acoustic analysis** — No real-time pitch extraction; depends on external scorer  

### Architectural Trade-offs (By Design)
| Constraint | Reason | Impact |
|---|---|---|
| **No acoustic I/O** | FeedBack separates audio from scoring | We can't tune dynamic difficulty without a scoring plugin active |
| **Phrase-boundary-only adjustment** | Stability over responsiveness | Smoother experience, but less frequent updates (good for practice, not ideal for arcade-like flow) |
| **Step-based adjustment** | Simpler UX, easier to predict | Jumps are visible; Rocksmith's curve is smoother |
| **Heuristic generation** | No closed-source magic; reproducible | Quality depends on heuristic tuning; hand-authored ladders are gold standard |
| **4 levels default** | Balances coverage vs. authoring burden | Rocksmith's 2–31 range gives more granularity but requires more work |

---

## Upgrade Roadmap (evidence-grounded, post-code-audit)

The first pass at this section was written from the outside — plausible engineering guesses about what's blocking real-time/confidence-based scoring. Reading `screen.js`, `routes.py`, `highway.js`, `highway-state-primitives.js`, and the `note-detection` capability domain (`static/capabilities/note-detection.js`) directly changes several of those guesses. Corrections first, then a prioritized upgrade list.

### Three corrections to the original analysis

1. **`alpha` is not a confidence score — don't try to read it as one.** The note-state provider contract (`static/js/highway-state-primitives.js:60-78`) does carry a numeric `alpha` field, and I initially flagged this as an untapped confidence signal. It isn't: the contract explicitly defines `alpha` as *fade-timing for the visual glow* ("You own all fade timing: return a decaying `alpha` for a struck-note glow, `alpha: 1` for a held sustain"), not hit quality. `_noteState()` also **hard-validates** the state enum to exactly `'hit' | 'active' | 'miss'` (line 66) — any richer verdict a scorer might want to send is structurally impossible through this channel today. Multi-dimensional (pitch/timing) scoring is off the table until `note_detect` (or core) ships a different channel — it's not ours to add unilaterally.

2. **The `note-detection` capability domain (spec 009) exists but isn't a shortcut here.** Core does expose a richer primitive-level domain (`pitch.estimate`, `verify.target` via `open-binding`/`set-target`) that in principle could give a consumer real pitch/timing data instead of a pre-judged verdict. But per its own doctrine, *consumers own all judgment semantics* — using it would mean `dynamic_difficulty` reimplementing hit-window/streak/accuracy logic that's `note_detect`'s job, not ours. More importantly, `note_detect` itself hasn't migrated onto it yet — the domain file's own compatibility-shim code (lines 303–340) exists specifically to instrument the *legacy* `highway.setNoteStateProvider` path, confirming the chart-coupled scorer is still on the old contract. There's nothing to bind to yet that would give us more than we already get.

3. **"Smooth adjustment curve" has less room than it sounds like.** Phrase note content is pre-quantized into discrete integer tiers at generation time (`routes.py`'s `generate_phrases_for_arrangement` → `phrases[].levels[]`, indexed `0..max_difficulty`), and tier *selection* from the mastery percentage is a `floor()` (mirrored in our own glass HUD math, `screen.js:474-477`). A smooth ramp on the `%` we hand to `setMastery()` would make the *slider* glide instead of jump, but the instant it crosses a tier boundary the actual notes on screen still cut over in one frame — there's no blended "80% of the way from tier 2 to tier 3" note set. True continuous density scaling would need core-level interpolation between adjacent tiers, which is out of scope for a plugin.

### Recommended — low effort, self-contained, real value

| # | Upgrade | Where | Effort | Why |
|---|---|---|---|---|
| 1 | **Wire up ladder granularity to the Generate button** | `screen.js` `currentTarget()`/`onGenerateClick()`, `routes.py`'s `/generate` already accepts `levels` (2–8, `routes.py:657`) | Low | The backend already supports variable tier counts — the frontend just never sends the field, so every generated ladder defaults to 4. Add a settings.html slider (2–8) and thread it into the POST body. Immediate, honest partial closure of the "we only ever generate 4 levels" gap (still capped well under Rocksmith's 31 — see caveat below). |
| 2 | **Warm-up window before auto-adjust acts** | `commitPhraseResult()`, `screen.js:242` | Low | Nothing currently stops auto-adjust from reacting to phrase 1 of a cold, nervous first attempt. Track a per-song `_phrasesScored` counter, gate the auto-adjust branch on a small configurable minimum (default ~2) before the first automatic move. |
| 3 | **Ramp instead of jump on a committed step** | `commitPhraseResult()`, `screen.js:267-282` | Low | Once a threshold crosses, `next = curPct ± th.step` moves the full 10–20% in one call. Splitting that into 2–3 smaller moves over consecutive qualifying phrases reads as "adapting to you" rather than "lurching." Self-contained — no new state beyond a target/current split. |
| 4 | **Count `active` states in phrase scoring, not just `hit`/`miss`** | `tickScoring()`, `screen.js:364` | Low–Medium | Confirmed by direct read: today `if (nname === 'hit' || nname === 'miss')` — any note the provider is currently reporting as `'active'` (a sustain being held correctly) contributes **nothing** to `_phraseTotal`/`_phraseHits` unless it later resolves to a terminal hit/miss key. Long sustained passages may be under-counted. Worth a deliberate design decision (credit `active` at phrase-end if still active, or leave as-is because `note_detect`'s own hit/miss accounting already covers the onset) rather than the current silent gap. |

### Worth considering, lower confidence of payoff

- **Direction-asymmetric step size** (larger downward steps than upward) — a common adaptive-difficulty UX pattern in general, *not* something confirmed about Rocksmith specifically from the source material in this thread, so frame it as our own design choice rather than parity-seeking. Cheap to add to `thresholds()`.
- **"Mastery streak" badge** — when EMA stays at/above the up-threshold *at max difficulty* for several consecutive phrases, surface a small state change on the existing glass HUD canvas (already redrawn every frame — no new render hook needed). A cheap, honest nod to the spirit of Master Mode without touching the renderer contract, fading notes, or the 110%-score concept, none of which fit this plugin's architecture.

### Still not worth doing (confirmed, not just assumed)

- ❌ **Master Mode (110% + fading notes)** — needs new render-layer hooks/overlay contract work; large lift for a practice-tool plugin.
- ❌ **Real-time (sub-second) adjustment** — would require `note_detect` to push a live per-note event stream our plugin could react to mid-phrase; no such event exists in the current highway/capability surfaces short of us duplicating `note_detect`'s own judgment loop.
- ❌ **Confidence/pitch/timing-weighted scoring** — blocked at the contract level (see correction #1), not a matter of effort.
- ❌ **2–31 levels per phrase** — technically raisable (`routes.py:657`'s `min(..., 8)` cap is just a number), but the percentile-bucketing heuristic (`_assign_levels`) needs enough distinct note groups per phrase to populate that many *meaningfully different* tiers. Most phrases don't have 31 groups worth of gradation; raising the cap without also reworking the heuristic would just produce duplicate/no-op tiers.

---

## Recommendation: Is "Rocksmith Parity" the Right Goal?

**Short answer: No, by design.**

Our plugin is optimized for **practice + observation**, not **arcade gameplay**. The differences aren't bugs—they're architectural choices:

1. **FeedBack is a practice tool** — Wait-for-section-end is actually ideal for deliberate learning. Rocksmith's real-time adjustments make the game feel responsive during play.

2. **We don't own the scorer** — Real-time adjustment quality depends entirely on note_detect's latency and accuracy. If it lags, realtime would be jittery.

3. **Heuristic generation is a fallback** — Hand-authored tiers (Rocksmith's model) are always better than generated ones. We fill gaps where authors haven't provided data.

4. **Simplicity wins in practice mode** — A 4-tier system is easier to reason about than 2–31 tiers.

---

## Implementation Notes for the Recommended Tier

- All four items in the "Recommended" table are self-contained within `dynamic_difficulty` — no changes to core, `note_detect`, or any other plugin required, and no new capability surface to design.
- Items 2–4 touch pure/testable logic (`commitPhraseResult`, `tickScoring`) that already has unit coverage in `tests/screen.test.js` (`thresholds()`, `emaAlpha()`, etc.) — new behavior should get the same treatment (add cases, don't just eyeball it).
- Item 1 (ladder granularity) is the only one touching `routes.py` + `settings.html` + the generate POST body together; still small — one new setting, one new field on an existing request.
- Per this repo's `CLAUDE.md`, any of these once actually shipped is a user-visible change and needs a `version` bump in `plugin.json` (currently `0.2.0`) — minor for the granularity control (new capability), patch-or-minor for the ramping/warm-up behavior depending on how it's framed to users.

---

## Conclusion

Our plugin and Rocksmith's engine **solve the same problem** (keep difficulty matched to skill) **via different means** (observation-based adjustment vs. real-time confidence scoring). Rocksmith is a **polished consumer product** tuned by Ubisoft across millions of players; we're a **modular practice tool** that sits on top of open, pluggable scoring.

**The gap is not a deficiency — it's a design philosophy**, and the code audit above confirms most of it is also a *contract* boundary, not just a priority choice: `getNoteStateProvider()` structurally cannot carry confidence data, and the one core mechanism that could (the spec-009 `note-detection` domain) doesn't have a migrated provider to bind to yet. Chasing real-time/confidence parity today would mean `dynamic_difficulty` reimplementing detection judgment that belongs to `note_detect` — the wrong plugin taking on the wrong responsibility.

What's actually left on the table is smaller and cheaper than the original pass suggested: expose the generation granularity the backend already supports, smooth out the two most visible edges of the current step-based adjustment (cold-start overreaction, one-shot jumps), and decide deliberately — rather than by accident — whether sustained `active` notes should count toward phrase accuracy. None of these require touching core.
