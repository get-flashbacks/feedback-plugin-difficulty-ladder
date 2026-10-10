# Adaptive difficulty: Phase 2 simulation scaffold

Research progress for [#55](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/55),
under parent [#31](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/31).
This completes the unblocked scenario/measurement scaffold. Candidate implementation/ranking
still waits on the Phase 1 review (#54); this is not a Phase 4 adoption report.

## Reproduce

Requires Node.js and this repository; no npm dependencies, audio, GPU or player data.

```bash
node --test
node tools/adaptive_simulation.js > baseline.json
node tools/adaptive_simulation.js --trace=fatigue --seed=1000 > fatigue.ndjson
node tools/adaptive_simulation.js --seeds=2 --sessions=2 --phrases=30 > smoke.json
node tools/adaptive_simulation.js --exploration=0 > no-exploration.json
```

Summary JSON includes every seed's metrics and calibration bins. Trace NDJSON begins with
configuration/source metadata, followed by each phrase's observations, pre-update prediction
and separate evaluation truth. It is synthetic data, not a production phrase-attempt export.
Unknown/duplicate flags and invalid sample sizes fail rather than producing misleading output.
Configuration fields beyond these CLI flags can be supplied through the exported JS API.

## Model and scope

- Twenty independent seeds (1000–1019), four scenarios, eight sessions per seed,
  100 phrases per session: **64,000 total synthetic phrase attempts**.
- Nine items have fixed base difficulty from -1 to +1, equally spaced. There are eleven
  synthetic tiers (0–10): `floor(masteryPct / 100 * 10)`. This is an explicit synthetic
  tier map, not a certification of any real chart's tier semantics.
- Item difficulty on each attempt is `baseDifficulty + 0.35 * presentedTier`.
  Skill and difficulty are on a shared synthetic logit scale. Per-note success probability
  is `sigmoid(skill - difficulty)`; hits are independent binomial trials.
- Each phrase draws 6, 16 or 40 judged notes with equal probability. The item-selection,
  response and day-offset random streams are separate and seeded.
- Ordinary selection cycles through the middle three items. With probability 0.2,
  exploration chooses uniformly from the full bank (including those middle items).
  This jitters **item selection**, not the slider: arbitrary slider drift would trigger
  the real controller's manual-override guard. It provides coverage without bypassing that guard.
- Fast learner: rapid exponential improvement; plateau: initial linear improvement followed
  by flat skill; fatigue: a repeated within-session decline with recovery next session;
  day variance: a gradual trend plus seeded daily offsets. Exact functions are in `skillAt`.
- The actual `screen.js` EMA/threshold controller selects mastery. The CommonJS exports run
  in an isolated VM per run. Default sensitivity/reaction are 2, drop resistance is off,
  down-step ratio is 1, and starting mastery is 50%. Every session represents a song boundary:
  EMA resets as in production while the difficulty slider carries over.
- A prediction is the **previous** EMA, or neutral 0.5 on a fresh session. The outcome then
  updates the shipped controller. No post-update fitted value is scored as a forecast.
- This bypasses live judgment polling, phrase finalization/persistence and Elo logging.
  It tests the EMA commit/selection path, not all of the instrumentation added in #53/#113.

The source SHA-256 is recorded in every output. These results used controller commit
`890ad86643d2a369b31fddb1a615c1019a1d79fd` (including merged #113):
`fde6c78291e5c84314670cddc3cc214fb5921c5283778d47155cfa510bd884fe`.
Regenerate if the controller/configuration changes; do not silently compare old and new outputs.

## Measured EMA baseline

Executed with Node.js v24.19.0 using the default command above. All phrases, including cold starts,
are scored; there is no burn-in exclusion. Values below are means across twenty runs. Min/max
are the range of **run-level** RMSE, not confidence intervals or pooled phrase quantiles.
Seed-level scalar metrics and convergence counts are retained in
[`research/adaptive_baseline.json`](research/adaptive_baseline.json);
the command regenerates full calibration bins and traces on demand.

| Scenario | Probability RMSE | Run RMSE min–max | Per-note Brier | Movement SD (percentage points) | Moves / 100 phrases |
|---|---:|---:|---:|---:|---:|
| Fast learner | 0.098865 | 0.090998–0.105324 | 0.173352 | 1.718268 | 11.875000 |
| Plateau | 0.099567 | 0.092582–0.107041 | 0.178177 | 1.639182 | 10.818750 |
| Fatigue | 0.104861 | 0.100852–0.109301 | 0.183681 | 1.957214 | 15.362500 |
| Day variance | 0.099051 | 0.094075–0.103769 | 0.174994 | 1.762770 | 12.493750 |

These are measured outputs of the declared synthetic model. They do not establish the performance
of actual players, the optimal training accuracy, or a candidate's advantage over EMA.

## Metrics and comparison contract

- **Probability RMSE:** unweighted per-phrase pre-update prediction error against known true
  success probability. This is a common forecast scale for different estimators.
- **Skill/item RMSE:** `null` for EMA. It estimates a rate, not latent skill or item difficulty.
  A future candidate must state its logit/Elo scaling and identification anchor before reporting
  these values. Subtracting an EMA hit rate from a latent skill value is not a valid comparison.
- **Brier:** per-note squared error, weighted by judged-note count, computed from hit/miss counts.
- **Calibration:** signed note-weighted predicted-minus-observed rate, plus ten probability bins.
  Bins with no notes report `null`, not a zero accuracy. Correlated misses are not modeled here.
- **Oscillation:** population SD of within-phrase slider changes (percentage points), alongside
  moves per 100 phrases. Session boundaries are not counted as difficulty jumps.
- **Convergence:** first completed twenty-phrase window with forecast RMSE at most 0.1, separately
  for each session. `null` means it never occurred. For changing skill this is only the first
  low-error window, not evidence of permanent convergence or skill identification.

`replay(run, factory)` is the extension seam for Phase 1-selected estimators. `factory()` returns
`predict(context)` and `update(outcome)`. Prediction sees only session/phrase/item/tier/note count;
the observed hits/misses arrive afterward. Evaluation truth is never passed to either method.
Each replay gets a fresh estimator, and malformed predictions fail. An oracle probability trace
is used only as a test of the scorer (zero RMSE), never as a deployable candidate.

All adapters replay the same **EMA-selected** item/tier/outcome stream, preserving a paired
forecast comparison. This is not candidate-controlled closed-loop selection. Future work must
evaluate each candidate's selection policy separately with common random draws; a replay gain
alone cannot establish the #31 adoption threshold or an oscillation advantage for that candidate.

## Remaining gates

1. Complete #54's source-backed candidate selection and parameter/observation models.
2. Add the selected Elo/Glicko, binomial/logit Kalman and IRT adapters; predeclare tuning vs.
   evaluation seeds. Evaluate calibration priors, item coverage and tier-aware item identities.
   The shipped #113 logger keys by phrase and seeds from relative `difficulty_cost`; neither
   that prior nor its prediction is assumed calibrated by this scaffold.
3. Anchor latent skill/item scales before interpreting tracking RMSE; retain probability error
   as a common comparison metric. Assess correlated phrase errors and richer/chart-derived items.
4. Compare candidate-controlled runs, not just replay forecasts, before shortlisting in #55.
5. #56 still requires real-player logs and its study-design choice; #57 owns the adopt/defer
   decision after simulation and human guardrails. No retention evidence was generated here.

No runtime plugin files, settings, storage layouts or production difficulty behavior are changed.
The new tests verify deterministic scenarios, binomial-count consistency, prequential scoring,
session isolation, exploration, truth withholding, oracle metrics, sample-size validation and CLI
output. `node --test` passes 345 tests (335 existing + 10 scaffold tests).
