# Adaptive difficulty: Phase 2 simulation evaluation

Computational phase for [#55](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/55), under [#31](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/31). It implements the Phase 1-selected estimator candidates, tunes only on declared development seeds, evaluates on held-out seeds, and compares both prequential forecasts and synthetic candidate-controlled runs. **This is not a Phase 3 human benchmark, an adoption decision, or evidence of learning/retention.**

No production controller, setting, storage or runtime behavior changes.

## Reproduce

Requires Node.js and this repository; no npm dependencies, audio, GPU or player data.

```bash
node --test
node tools/adaptive_simulation.js --output=research/adaptive_baseline.json
node tools/adaptive_simulation.js --mode=compare --output=research/adaptive_candidates.json
node tools/adaptive_simulation.js --mode=compare --train-seeds=2 --eval-seeds=2 --sessions=2 --phrases=30 --output=/tmp/adaptive-smoke.json
node tools/adaptive_simulation.js --trace=fatigue --seed=1000 > /tmp/fatigue.ndjson
```

Default tuning seeds are 1000–1019; held-out evaluation seeds are 2000–2019. CLI seed counts take the first N seeds from those disjoint ranges. `--output` writes the JSON/NDJSON directly; omit it to print to stdout. Both checked-in result files include source SHA-256 values. Regenerate them if the controller, simulator, estimator or configuration changes.

## Synthetic data model

- Four synthetic skill trajectories: fast learner, plateau, within-session fatigue with recovery at the next session, and day-to-day variance. Exact functions are in `skillAt`.
- Nine items expose two centered synthetic chart features: density and interval complexity. **Ground-truth** base difficulty is `0.55 × density + 0.45 × interval`, so the item-bank mean is anchored at zero. The LLTM learns these weights from the features; it is intentionally a correctly specified/optimistic feature-model case, not a claim that real chart difficulty has this feature law.
- Eleven discrete tiers (0–10); each tier adds a known `0.35` to item difficulty on the shared logit scale. Per-note probability is `sigmoid(skill − itemDifficulty)`.
- Each phrase has 6, 16 or 40 judged notes; outcomes are binomial hit/miss counts. Item selection cycles through the middle three items, with 20% uniform exploration of the whole bank. Separate seeded streams generate item choices, note counts, responses and day offsets.
- One reference scenario/seed comprises eight sessions × 100 phrases. Tuning comprises 4 × 20 × 800 = **64,000** reference-stream attempts. Evaluation uses another 64,000 EMA reference-stream attempts, plus 4 × 4 × 20 × 800 = **256,000 candidate-controlled attempts**. Candidate replays reuse the paired EMA stream rather than regenerating outcomes.

### Compared models

- **EMA baseline:** the shipped `screen.js` controller runs in an isolated VM; its EMA resets at session/song boundaries, while the slider carries over. Default sensitivity/reaction are 2, drop resistance is off and the start is 50%. Its prediction is pre-outcome EMA (or 0.5 at a cold start).
- **Elo / online Rasch:** player ability and per-item base difficulty update together after each phrase, with symmetric K=32 updates in standard Elo's 400-point scale (converted to natural-logit units). The mean estimated base-item difficulty is centered at zero; the known tier effect is added separately. Note-count reliability is weighted by `sqrt(n/16)`.
- **Uncertainty-scaled Elo:** same model, with K multiplied by `1/(1 + 0.06 × prior phrase count)` for player and item. This tests the simple dynamic-K variant from Phase 1, **not a full Glicko implementation**.
- **Kalman-Elo:** online Elo item estimates plus a scalar Kalman ability state. The observation is the smoothed binomial logit, `logit((hits + 0.5)/(n + 1)) + estimated item difficulty`; observation variance uses the delta-method binomial variance plus an item-uncertainty term. Process noise is tuned on training seeds.
- **LLTM:** online binomial-logistic ability + linear item-feature difficulty. It uses the exact binomial score `hits − n × p`, normalized by 16 notes, with the centered density/interval features. Learning rate is tuned on training seeds. The mean-zero feature anchor identifies the ability/item scale.

Candidates receive item IDs, the synthetic feature vector, tier and note count before prediction; hit/miss counts arrive only in the subsequent update. They never receive the hidden skill, true item difficulty or response probability. The replay API tests that boundary.

## Comparison protocol

1. **Tuning:** Kalman process noise from `{0.002, 0.01, 0.02, 0.05, 0.1}` and LLTM learning rate from `{0.16, 0.24, 0.3, 0.4, 0.5, 0.6}` are selected by lowest pooled prequential probability RMSE across training seeds 1000–1019 and all four scenarios. Elo K and dynamic-K `β=0.06` are fixed from the literature/design. Selected values: Kalman `q=0.01`; LLTM `η=0.4`.
2. **Held-out replay:** each estimator predicts the same EMA-selected item/tier/outcome stream for evaluation seeds 2000–2019. This isolates forecasting but does not test the candidate's own selection policy.
3. **Candidate-controlled:** separately, each estimator selects a tier that minimizes distance to the declared 0.75 predicted-success target for the selected item. All candidates share item/note/response random streams, warm up for two phrases, confirm a direction for two phrases, wait three phrases between steps, and move at most one tier per action. This is a synthetic comparison policy, not a production controller. The EMA baseline continues to use the actual shipped controller.

No evaluation seed participates in tuning. `research/adaptive_candidates.json` retains the full run-level metrics, pooled calibration bins, seed lists, tuning trials, configuration and source hashes.

## Metrics

- **Probability RMSE:** unweighted per-phrase error of the pre-update success-probability forecast.
- **Skill / item RMSE:** latent logit-scale tracking errors after the explicit mean-item-difficulty anchor. These are `null` for EMA, which estimates a rate, not latent ability/item difficulty.
- **Brier / calibration:** note-weighted Brier, signed note-weighted predicted-minus-observed hit rate, and ten predicted-probability bins. Bins without notes are `null`.
- **Movement / oscillation:** population SD and variance of within-phrase slider changes in percentage points, plus moves per 100 phrases. This measures the tested controller policy, not learner experience.
- **Convergence:** first completed 20-phrase session window with forecast RMSE ≤0.1. It is only the first low-error window for a changing synthetic trajectory, not permanent convergence or proof of skill acquisition.

## Held-out results

Means across the 20 evaluation seeds for each scenario; the first table compares candidate-controlled selection against the current EMA controller.

| Scenario | EMA | Elo | Dynamic-K Elo | Kalman-Elo | LLTM |
|---|---:|---:|---:|---:|---:|
| Fast learner | 0.1177 | 0.0505 | 0.1247 | 0.0573 | 0.0354 |
| Plateau | 0.1200 | 0.0474 | 0.0889 | 0.0571 | 0.0348 |
| Fatigue | 0.1257 | 0.0920 | 0.1044 | 0.0700 | 0.0739 |
| Day variance | 0.1187 | 0.0637 | 0.0934 | 0.0599 | 0.0486 |

Probability RMSE, averaged over the four scenario means, is **0.1206 EMA**, **0.0634 Elo**, **0.1028 dynamic-K Elo**, **0.0611 Kalman-Elo**, and **0.0482 LLTM**. In held-out replay, corresponding means are 0.1206, 0.0624, 0.1088, 0.0603 and 0.0460. Thus the replay ordering broadly survives the shared candidate-controlled policy; this is still synthetic forecast accuracy, not learning gain.

| Candidate-controlled model | Skill RMSE | Item RMSE | Per-note Brier | Movement SD (pp) | Moves / 100 | Converged sessions* | Mean first convergence (phrases) |
|---|---:|---:|---:|---:|---:|---:|---:|
| EMA baseline | — | — | 0.1775 | 1.81 | 13.18 | 144.5 / 160 | 39.81 |
| Elo | 0.3445 | 0.2756 | 0.1815 | 3.49 | 12.18 | 160 / 160 | 23.12 |
| Dynamic-K Elo | 0.7616 | 0.3644 | 0.1595 | 3.23 | 10.46 | 129.25 / 160 | 33.53 |
| Kalman-Elo | 0.3129 | 0.2584 | 0.1760 | 3.44 | 11.83 | 160 / 160 | 20.85 |
| LLTM | 0.2193 | 0.1285 | 0.1853 | 3.73 | 13.95 | 160 / 160 | 20.98 |

*Mean count across the four scenarios; each scenario has 160 held-out sessions (20 seeds × 8 sessions). Convergence window is RMSE ≤0.1 over 20 phrases.

## Phase 2 conclusion / Phase 3 shortlist

- **Advance Kalman-Elo as the primary estimator candidate** and fixed Elo as its reference. Kalman-Elo tracks fast learning, plateau and day variance near fixed Elo, and gives the best controlled-model forecast in fatigue; its binomial/logit observation model is the closest match to the observed counts.
- **Keep LLTM as an exploratory upper-bound candidate**, not a ready-to-ship winner. It wins this simulation because the synthetic item difficulty is deliberately generated from the exact two centered features the LLTM sees. Real-chart feature validity and calibration remain untested; its controller policy also moves more than EMA.
- **Do not advance the tested dynamic-K Elo setting as the lead candidate.** Its estimate adapts too slowly to changing skill in this model and it is the only candidate that fails the convergence threshold in a substantial fraction of sessions. This is a result about this fixed decay (`β=0.06`) and these trajectories, not a rejection of Glicko generally.
- **Do not adopt any candidate from this simulation.** The models estimate synthetic skill and forecast hits; this experiment measures neither retention nor transfer, player comfort, or real-chart difficulty calibration. Phase 3 (#56) still needs real-player data and its own study design; Phase 4 (#57) owns the adopt/defer decision.

## Remaining work

- #56: human benchmark on real player sessions, with real hit counts and a predeclared privacy/study design.
- #57: adopt/defer only after the simulation, human evidence and guardrails are considered.
- No retention outcome, music-learning effect, real `difficulty_cost` calibration, or production-quality item-feature model is established here.

`node --test` covers seeded scenario generation, variable binomial counts, truth withholding, per-candidate anchoring/metrics, prequential ordering, disjoint tuning/evaluation seeds, shared random numbers, deterministic CLI/output files and invalid-input rejection. The complete repository suite passes **398 tests**.
