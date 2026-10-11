# Adaptive difficulty: Phase 1 literature review

Research for [#54](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/54),
the Phase 1 gate of parent [#31](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/31).
It supplies the source-backed candidate list and the parameter/observation models that
[`ADAPTIVE_SIMULATION.md`](ADAPTIVE_SIMULATION.md) (Phase 2 scaffold, #212) named as its first
remaining gate. It does **not** pick a production algorithm, and it changes no runtime behaviour.

## How to read this

Each topic states its **core claim**, an **evidence grade**, the **transfer risk** to this plugin,
and **sources**. Grades are about the claim *as stated for its own domain*, not about whether it
transfers here:

- **High** — formal derivation, replicated result, or broad consensus.
- **Moderate** — one solid study, or converging-but-limited evidence.
- **Low** — single small study, self-reported, or an extrapolation across domains.

Every grade above the topic line is deliberately conservative about **transfer to continuous
instrument practice**. The recurring problem for all of these models is that the evidence base is
built on *discrete, right/wrong testing items* (arithmetic, geography facts, vocabulary, binary
image classification), while this plugin measures *continuous motor performance* of a musical
phrase. That gap is flagged in every section and summarized in the last section.

## 1. Elo/Glicko as online Rasch estimation

**Core claim.** A player-vs-item Elo update is a one-pass, online approximation of the 1-parameter
Rasch (IRT) model. Both the learner's ability and the item's difficulty are updated after every
answer, so item difficulty needs **no pre-testing** — the basis of "on-the-fly item calibration".

The update (Pelánek/Papoušek) is a "match" between learner and item:

```
θ_player := θ_player + K · (correct − P(correct | θ_player, d_item))
d_item   := d_item   + K · (P(correct | θ_player, d_item) − correct)
```

with `P = sigmoid(θ − d)` on a shared logit scale. A constant `K` is the naive version; the useful
version replaces it with an **uncertainty function** `U(n) = α / (1 + βn)` (n = prior updates), so
early answers move a rating more than late ones.

**Evidence grade: High** for "Elo ≈ online Rasch". Reported figures:

- The Elo estimate correlates **r = 0.97** with joint maximum-likelihood Rasch estimation (JMLE),
  at a fraction of the cost, and is trivially online (Pelánek et al. 2017).
- Meta-parameters **α = 1, β = 0.06** (prior-knowledge model) and **α = 1, β = 0.05** (current-
  knowledge model) were grid-searched on real geography data; the authors stress the exact values
  are not critical — "many different choices … provide very similar results" (Pelánek et al. 2017;
  Pelánek 2016). Earlier student modeling used a constant `K = 0.4` (surveyed in
  Pelánek 2016).
- **Math Garden** (Klinkenberg, Straatemeier & van der Maas 2011) runs the largest deployed case:
  3,648 children, >3.5M arithmetic problems, ability *and* item difficulty updated every response.
  It uses a **dynamic K**: default `K = 0.0075`, with weights `K⁺ = 4` / `K− = 0.5` scaled by an
  uncertainty that decays to 0 after 40 administrations and resets toward 1 after 30 days idle.
- **Glicko** (Glickman 1999) is the same family with an explicit rating deviation `RD` — the
  principled Bayesian uncertainty version. Pelánek's review notes it can replace `U(n)`, but found
  simpler uncertainty functions "do not bring significant advantages" for prediction.

**Timeout/setpoint.** Math Garden deliberately samples items at **mean success probability 0.75**
("challenging yet not too difficult"), and folds response time into the score. A dynamic-K study
([Springer, 2025](https://link.springer.com/article/10.1007/s11257-025-09439-z)) reports faster
convergence when K is tuned to learner behaviour.

**Critical caveat (directly relevant here).** "Keeping Elo alive" (Utrecht, 2025) shows the
educational Elo estimate has non-trivial **bias, variance inflation and convergence problems
under adaptive item selection**, and that standard Elo's measurement guarantees assume a 0.5
setpoint. At a non-0.5 setpoint (e.g. Math Garden's 0.75), Elo's *assessment* bias changes and
the estimate can drift over time; the paper proposes mitigations (e.g. parallel/anchored rating
tracks) to keep the latent estimate identifiable.

**Transfer risk.** Elo-for-education is well supported **for discrete items with many attempts per
item**. Two transfer issues here:
1. Our "items" are phrases; the same phrase recurs far less often than an arithmetic fact, so item
   difficulty converges slowly (Pelánek: item difficulty needs on the order of ~100 responses).
2. Our outcome is a **hit rate over a phrase** (a count), not a single Bernoulli — the observation
   model must be binomial (see §3).

**Relevance to this repo.** This is already the incumbent: #113 shipped a per-player + per-phrase
Elo, seeded from the generator's relative `difficulty_cost`, **logged only**. The scaffold's gate
text explicitly says "neither that prior nor its prediction is assumed calibrated".

**Sources.** Klinkenberg et al. 2011, *Computers & Education* 57(2):1813–1824
([doi](https://doi.org/10.1016/j.compedu.2011.02.003)); Pelánek 2016, *Computers & Education*
98:169–179 ([pdf](https://www.fi.muni.cz/~xpelanek/publications/CAE-elo.pdf)); Pelánek et al. 2017,
*UMUAI* 27 ([pdf](https://www.fi.muni.cz/~xpelanek/publications/umuai-adaptive-practice.pdf));
Glickman 1999 (Glicko); "Keeping Elo alive" 2025
([pdf](https://research-portal.uu.nl/ws/files/234537893/Elo_stability_inflation_and_convergence-3.pdf)).

## 2. IRT / explanatory IRT (LLTM)

**Core claim.** Standard IRT (1PL/2PL) estimates a difficulty per item. The **Linear Logistic Test
Model** (LLTM, Fischer 1973) instead writes each item's difficulty as a **linear combination of
item features**, `d_i = Σ w_k · q_ik`, which lets the model **predict the difficulty of unseen
items** from their features — the cold-start case this plugin has on every new chart. Explanatory
IRT extends this to covariates and growth.

**Evidence grade: High** for the LLTM formalism; **Low/Moderate** for cross-domain transfer.
Modern work predicts item difficulty from features (including text/BERT-based feature models),
but the validated domains are text/reasoning items.

**Transfer risk.** Two real problems:
1. No music/motor LLTM is validated. The *form* is domain-agnostic, but the **feature
   engineering** (which phrase properties drive difficulty) would be ours and untested.
2. It needs a per-item response matrix to fit. Until enough phrases have been seen, LLTM is a
   long-run layer, not a cold-start-from-nothing solution — though its feature prior *is* the
   principled version of what we already do crudely.

**Relevance to this repo.** The generator's `difficulty_cost` (#72/B1) and the planned
per-technique weighting (`TECHNIQUE_WEIGHTING.md`, C7-1 / #167) are effectively a hand-built,
relative LLTM. The Elo seed in #113 is an ad-hoc feature prior. LLTM is the principled target
that unifies both — but it is a Phase 2+ direction, not a drop-in.

**Sources.** Fischer (1973), LLTM; Kadengye et al. (2018), explanatory IRT for growth; De Boeck &
Wilson, *Explanatory Item Response Models* (2004).

## 3. Kalman filtering as a refinement layer

**Core claim.** Ability tracking is recursive Bayesian estimation of a latent state. The shipped
EMA is not a rival to a Kalman filter — it **is** a special case of one: the **local level model**
(random walk + noise) with the Kalman filter is algebraically equivalent to simple exponential
smoothing, and the "adaptive GAIN" of the LLM is what EMA's fixed α approximates. A
time-varying-parameter model gives a principled, ML-estimatable **variable gain** (Adaptive Least
Squares nests the LLM).

**Evidence grade: High** for (a) EMA ↔ local-level-model equivalence and (b) the scaling fix below;
**Moderate** for practical gain over a well-tuned Elo.

**The load-bearing technical point.** The naive Kalman uses a **linear-Gaussian observation**
model `obs = state + noise`. A phrase's outcome is a **binomial count** (k hits in n notes), so
applying the raw-hit-rate Kalman is misspecified. The correct fix is to run the filter on the
**logit** of success (transform via the **delta method**, or use an **EKF / Laplace / assumed-
density** filter). The scaffold already speaks this language (["Skill/item RMSE … a future
candidate must state its logit/Elo scaling and identification anchor before reporting these
values"], plus the per-note Brier and calibration bins).

Ye et al. (2026) add a caution: learning a Kalman filter's **latent state** from outputs alone is
provably hard — they show no algorithm can achieve sublinear regret in the horizon `T` (i.e. the
regret is `Ω(T)`) — and it becomes tractable only with **informative measurements** (here: the
logit transform, and the count `n`) via a limited-query scheme. This argues for the logit/binomial
route, not raw rates.

**Transfer risk.** Low — this is a statistical layer, domain-independent. The only risk is
mis-specifying the observation model, which the scaffold's metric design already guards against.

**Relevance to this repo.** This is the natural **refinement layer** over the Elo candidate:
replace "EMA on hit rate" with "state-space filter on logit success with a binomial observation",
and expose `(estimated state, uncertainty)` to selection. It also gives a principled way to
express the deconfounded `ability − difficulty` state the issue asks for.

**Sources.** Muth (1960) / Kalman (1960); Harvey (1989) *Forecasting, Structural Time Series*;
"Kalman Foundations of Adaptive Learning" (Ohio State,
[pdf](https://www.asc.ohio-state.edu/mcculloch.2/papers/KalmanAL.pdf)); statsmodels exponential-
smoothing-as-state-space ([docs](https://www.statsmodels.org/dev/generated/statsmodels.tsa.statespace.exponential_smoothing.ExponentialSmoothing.html));
Ye, Zhang, Chi, Du & Hu (2026), "Online Learning of Kalman Filtering: From Output to State
Estimation", arXiv:2603.27159 ([abs](https://arxiv.org/abs/2603.27159)) — output estimation is
`log T`-regret, but state estimation admits no sublinear-regret algorithm (the `Ω(T)` bound cited
above) unless given limited queries to more informative measurements.

## 4. The item-difficulty confound in adaptive systems

**Core claim (the #31 motivation).** Under **adaptive selection**, the system keeps choosing items
near the learner's ability. The observed **success rate therefore converges to the selection
setpoint regardless of ability** — a fixed, say, 0.75 success rate says almost nothing about *how
able* the learner is. This is the item-difficulty confound: raw accuracy stops being a measure of
ability once the adaptive loop is closed.

**Evidence grade: High.** It is an explicit, measured property of Math Garden and the reason its
designer uses **latent ability/difficulty estimates** (updated Elo), not proportion-correct, for
monitoring; the 2025 Elo paper formalizes the identifiability/stability problems this creates and
the need for anchors or parallel tracks.

**Transfer risk.** The confound is **structural, not domain-specific**, so it transfers directly.
This is the strongest reason the *latent-model* candidates (Elo, logit filter) beat "tune the EMA
threshold": the controller's whole job is to keep hit rate near a setpoint, so hit rate cannot be
its own success metric.

**Relevance to this repo.** This is exactly why #113 logs an Elo **difference** (`skill − item`)
instead of relying on EMA hit rate, and why the scaffold's convergence metric is explicitly
"only the first low-error window, not evidence of permanent convergence or skill identification".
It also justifies the scaffold's warning that **replay ≠ closed-loop**: a candidate's selection
policy must be evaluated with common random draws, because selection is what creates the confound.

**Sources.** Klinkenberg et al. 2011 (setpoint 0.75); "Keeping Elo alive" 2025; Pelánek 2016.

## 5. Flow theory & the DDA evidence base (mixed)

**Core claim.** Dynamic difficulty adjustment is widely claimed to improve engagement/enjoyment
via flow. **The evidence is mixed and the effect is not as clean as the design literature implies.**

**Evidence grade: Moderate** for DDA improving *performance*; **Low** for DDA improving
*enjoyment/flow*.

- Systematic reviews trend positive but heterogeneous: a video-game SLR (Zohaib 2018) and a
  serious-games SLR ([MDPI 2026](https://www.mdpi.com/2078-2489/17/1/96), 75 papers) report most
  studies "successful"/"promising" — **but** the MDPI review did **no risk-of-bias assessment**,
  and flags small samples and weak evaluation.
- Controlled studies are more sobering: a 3-arm study (DDA vs incremental vs no adjustment,
  N = 221) found DDA improved **performance** but had **no significant effect on flow or
  enjoyment** (Robb & Zhang, *Acta Ludologica* 2022). Other controlled work likewise reports
  drawbacks (e.g. a reduced sense of control) or no enjoyment gain even where performance
  improved.

**Transfer risk.** High — all of this is **games**, optimizing moment-to-moment engagement. This
plugin optimizes **skill acquisition during practice** (§6), which the flow/DDA game literature
does not measure (no retention/transfer outcomes).

**Relevance to this repo.** Treat flow/DDA as a *motivation*, not evidence. It should not be the
basis for a candidate's learning claim.

**Sources.** Zohaib 2018, *Adv. HCI* ("DDA in Computer Games: A Review"); Mortazavi et al. 2024,
*Multimedia Tools & Applications* ([doi](https://doi.org/10.1007/s11042-024-18768-x)); MDPI
*Information* 2026, DDA in serious games SLR, 75 papers, no risk-of-bias assessment
([mdpi.com/2078-2489/17/1/96](https://www.mdpi.com/2078-2489/17/1/96)); Robb & Zhang 2022,
*Acta Ludologica* 5(1):4–22
([pdf](https://actaludologica.com/wp-content/uploads/2022/06/AL_2022-5-1_Study-1_Robb-Zhang.pdf)).

## 6. Challenge point, desirable difficulties, and the 85% rule

**Core claims (practice-specific, not engagement-specific).**

- **Challenge point** (Guadagnoli & Lee 2004): learning is an **inverted-U** function of *functional*
  task difficulty (difficulty relative to the performer), so the optimal challenge changes as skill
  changes. This is a **motor-learning** framework.
- **Desirable difficulties** (Bjork): performance *during* practice is a poor proxy for learning;
  difficulties that force encoding/retrieval support retention/transfer — **but only if the learner
  is equipped**, otherwise they are *undesirable*. Bjork explicitly lists "adaptive learning
  schedules … tailored to an individual's past successes" as a good instantiation.
- **The 85% rule** (Wilson et al. 2019, *Nature Communications*): for gradient-descent learners on
  binary classification, the optimal training error rate is 15.87%, i.e. **~85% accuracy**.

**Evidence grade.** Challenge point: **Moderate** (influential, motor-learning-backed, but the
"optimal point" is hard to pin down). Desirable difficulties: **Moderate** (robust for spacing/
interleaving/testing; the specific link to moment-to-moment item difficulty is looser). 85% rule:
**High as a mathematical result**, **Low for human music transfer** — the authors derive it for
gradient-descent/binary classification and demonstrate it on artificial and biologically plausible
**neural networks**, not on human motor or musical practice. It is the origin of the "~85% target
accuracy" heuristic, not a validated music-practice constant. Notably a published music-practice
tool chains these framings while explicitly warning that applying 85% to instrumental practice is
"an extrapolation, not a direct empirical finding for musicians" (ModusPractica Pro, "The
Science", <https://www.parturamusic.com/ModusPractica_Pro/science.html>).

**Motor-sequence evidence that *does* support challenge point.** Wadden et al. 2018
(*J. Motor Behavior*) used a computer-controlled **learner-adapted** schedule derived from each
participant's own acquisition curve; the **high-difficulty/high-contextual-interference** condition
produced the best **retention**. This is the closest direct support for "adapt to an individual
challenge point" in a motor task, though the task is a serial reaction-time task, not music.

**Transfer risk.** **Partial** — better than flow/DDA because challenge point and the 85% rule are
about *learning*, and Wadden is *motor* learning. Still, none is *instrument practice*, and all
measure **delayed retention**, which the scaffold does not (it has no retention outcome; #56 owns
real-player evidence).

**Relevance to this repo.** Gives a principled basis for choosing a **target success rate** for
selection (e.g. ~0.75 Math Garden vs ~0.85 challenge-point/85%-rule), and a rationale for *not*
chasing momentary fluency. It also explains why "improve practice" ≠ "maximize enjoyment".

**Sources.** Guadagnoli & Lee 2004, *J. Motor Behavior* 36(2):212–224
([pdf](https://coachingvb.com/wp-content/uploads/2019/01/Challenge-Point-A-Framework-for-Conceptualizing-the-Effects-of-Various-Practice-Conditions-in-Motor-Learning.pdf));
Bjork & Bjork 2020, *JARMAC*; Wilson et al. 2019, *Nat. Commun.*
([pdf](https://www.nature.com/articles/s41467-019-12552-4)); Wadden et al. 2018
([pdf](https://msl.kin.educ.ubc.ca/sites/default/files/publication-files/Wadden_JMB_ChallengePoint_2018.pdf)).

## 7. Music / instrument-practice-specific literature

**Finding: there is music/instrument HCI work on adaptive feedback, but almost none validates a
specific adaptive difficulty *algorithm* for performance, and none for the candidates above.**

- **BACh** (CHI 2016): a brain-computer piano tutor raised difficulty when fNIRS-measured
  cognitive workload fell; learners played with significantly better accuracy/speed than control.
  Single small lab study, one learner group, no retention test.
- **FretFlow** (CHI 2026): adaptive haptics for guitar; adaptation *fades assistance* as timing
  stabilises rather than moving an item-difficulty parameter. N = 20, two weeks.
- **Challenge-point motor-sequence study** (Wadden 2018, §6) — the closest algorithmic support,
  but a serial RT task.
- **Deliberate practice in music:** meta-analytic effect of accumulated task-relevant practice on
  musical achievement **r ≈ 0.61** (Frontiers 2014 meta-analysis, 13 studies, N = 788), but the
  broader Macnamara et al. 2014 meta-analysis puts deliberate practice at only **21% of music-
  performance variance** (12% overall) — the 2018 corrigendum revises these to 23% and 14% — with a
  published Ericsson rebuttal. **Conclusion:** practice *quality/difficulty* plausibly matters, but
  the effect size is contested and the field has not produced a validated adaptive-difficulty law
  for instrumental practice.

**Transfer verdict (the issue's explicit ask).** Transfer from **discrete testing items to
continuous motor performance is unsupported** for the *quantitative* candidates (Elo setpoint,
IRT scaling, 85%). The *qualitative* claim — "practice difficulty tailored to the individual
improves learning" — is supported only by motor-learning literature (§6), and even there the
outcome is retention, which this plugin cannot yet measure. Any candidate's gain in the simulation
is therefore a **forecast-accuracy** result about a **synthetic** learner, not established
music-learning value.

**Sources.** BACh (CHI 2016, [doi](https://doi.org/10.1145/2858036.2858388)); FretFlow (CHI 2026,
[doi](https://doi.org/10.1145/3772318.3791540)); Frontiers 2014 DP meta-analysis
([doi](https://doi.org/10.3389/fpsyg.2014.00646)); Macnamara et al. 2014 (DP 21% music / 12%
overall; 2018 corrigendum 23% / 14%, doi:10.1177/0956797618769891).

## 8. Ranked recommendation for Phase 2

Ranking is by **(a) evidence in-domain, (b) transfer risk, (c) fit to the scaffold's `replay(run,
factory)` seam**, and **(d) whether the observation model is honest about counts**.

**1. Elo / online-Rasch with an uncertainty-scaled update — the incumbent, keep it as the
baseline candidate.** *High in-domain evidence; direct fit; already shipped (logged only, #113).*
- Ability **and** item updated; `U(n) = α/(1+βn)`, start at the literature values **α = 1,
  β = 0.06** (Pelánek; re-fit only after data exists — the authors say the exact choice is not
  critical). Alternative Math Garden dynamic `K` (`K = 0.0075`, `K⁺ = 4`, `K⁻ = 0.5`, U decaying
  over 40 administrations / 30 idle days) as a second parameterization.
- **Identification anchor is mandatory** (§4): fix the item-bank mean or designate a reference
  phrase, or report only `θ − d`, not absolute ability. State the logit scale explicitly.
- **Declare the setpoint** (§6): pick one of ~0.75 (Math Garden) or ~0.85 (challenge point/85%
  rule) as the **selection** target; do not assume 0.5. Note the 2025 caveat that Elo's assessment
  bias is setpoint-dependent.
- **Guard the adaptive-selection confound:** expect variance inflation/drift; plan anchored or
  parallel rating tracks as a mitigation to test.

**2. Binomial/logit state-space filter (Kalman/EKF on logit success) — the refinement layer.**
*High for the statistical construction; low transfer risk.*
- This is where the shipped EMA goes: EMA = constant-gain local level model. Move to a
  variable-gain filter on **logit** success with a **binomial** observation (delta method/EKF),
  not a raw-rate linear-Gaussian filter.
- Report `(state, uncertainty)`; keep probability RMSE + Brier + calibration as the common metrics
  the scaffold defines.

**3. Elo with dynamic K / Glicko RD — an uncertainty variant of #1.** *Moderate.* Worth one adapter
because it is cheap and the scaffold can compare it directly against fixed `U(n)`; but it adds
variance and inherits the setpoint caveat, so it is a variant, not a new candidate.

**4. Explanatory IRT / LLTM from phrase features — the cold-start + long-run item model.**
*High formalism, low validated transfer.* The principled version of `difficulty_cost` (#113 seed)
and `TECHNIQUE_WEIGHTING.md` (C7-1/#167). Requires a phrase feature vector and a per-item response
matrix. Include as a **stretch adapter** and as the plan for item difficulty once data accrues;
do not expect it to beat Elo on short streams.

**5. Defer.** Reinforcement-learning DDA (needs far more data than a single player generates) and
generic flow/DDA controllers (§5 has weak *learning* evidence, and we optimize practice, not
engagement).

**Parameter/observation-model checklist to hand #55 (the gate's actual ask):**
- Observation = **binomial counts** (hits/total per phrase), not one Bernoulli; report per-note Brier.
- Ability and item on a **shared logit scale** with an **explicit identification anchor**.
- **Uncertainty** exposed (`U(n)` or Glicko RD) so calibration can be evaluated.
- **Setpoint declared** as a selection target; justify 0.75 vs 0.85 from §6.
- Compare on **probability RMSE + calibration + oscillation**; latent RMSE only after anchoring.
- Evaluate **candidate-controlled closed-loop selection with common random draws**, not replay alone
  (§4: selection *is* the confound).
- Predeclare tuning vs evaluation seeds (scaffold gate #2).

## 9. What would change these conclusions

- **A music-specific adaptive-difficulty study** (even small-N) validating a setpoint or an Elo/IRT
  law for instrumental practice would move §7 from "unsupported" to "partial" and could reorder
  candidates.
- **Real-player logs (#56)** would let the same candidates be scored on real phrase attempts
  instead of the synthetic learner, and would test whether `difficulty_cost` seeding is calibrated.
- **Retention outcomes** would let the challenge-point/85% framing (§6) be tested directly, which
  the current scaffold cannot do.
- **An Elo stability analysis on our own data** would confirm or refute the §1 variance-inflation
  caution under *our* selection policy (which is closer to a hysteresis EMA than to Math Garden's
  per-item sampler).

## References

1. Klinkenberg, Straatemeier & van der Maas (2011). Computer adaptive practice of Maths ability…
   *Computers & Education* 57(2):1813–1824. doi:10.1016/j.compedu.2011.02.003
2. Pelánek (2016). Applications of the Elo rating system in adaptive educational systems.
   *Computers & Education* 98:169–179. https://www.fi.muni.cz/~xpelanek/publications/CAE-elo.pdf
3. Pelánek, Papoušek, Řihák, Stanislav & Nižnan (2017). Elo-based learner modeling for the
   adaptive practice of facts. *UMUAI* 27. https://www.fi.muni.cz/~xpelanek/publications/umuai-adaptive-practice.pdf
4. Dynamic K-factor extensions (2025). *UMUAI*. https://link.springer.com/article/10.1007/s11257-025-09439-z
5. "Keeping Elo alive" (2025). Stability, inflation and convergence of Elo in learning systems.
   https://research-portal.uu.nl/ws/files/234537893/Elo_stability_inflation_and_convergence-3.pdf
6. Glickman (1999). Parameter estimation in large dynamic paired comparison experiments (Glicko).
7. Fischer (1973). The linear logistic test model (LLTM).
8. De Boeck & Wilson (2004). *Explanatory Item Response Models*.
9. Guadagnoli & Lee (2004). Challenge point… *J. Motor Behavior* 36(2):212–224.
   doi:10.3200/JMBR.36.2.212-224
10. Bjork & Bjork (2020). Desirable difficulties in theory and practice. *JARMAC* 9(4):475–479.
11. Wilson, Shenhav, Straccia & Cohen (2019). The Eighty Five Percent Rule for optimal learning.
    *Nature Communications* 10:4646. doi:10.1038/s41467-019-12552-4
12. Wadden et al. (2018). Individualized challenge point practice… *J. Motor Behavior*.
13. Zohaib (2018). Dynamic difficulty adjustment in computer games: a review. *Adv. HCI*.
14. Mortazavi, Moradi & Vahabie (2024). DDA approaches in video games: a systematic literature
    review. *Multimedia Tools & Applications*. doi:10.1007/s11042-024-18768-x
15. MDPI *Information* (2026). Dynamic difficulty adjustment in serious games: a literature
    review (75 papers, no risk-of-bias assessment). https://www.mdpi.com/2078-2489/17/1/96
16. Robb & Zhang (2022). Performance-based DDA and player experience in a 2D digital game: a
    controlled experiment. *Acta Ludologica* 5(1):4–22.
17. Macnamara, Hambrick & Oswald (2014). Deliberate practice and performance: a meta-analysis.
    *Psychological Science* 25(8):1608–1618. doi:10.1177/0956797614535810 (corrigendum 2018:
    doi:10.1177/0956797618769891).
18. Platz et al. (2014). The influence of deliberate practice on musical achievement: a meta-analysis.
    *Frontiers in Psychology* 5:646. doi:10.3389/fpsyg.2014.00646
19. BACh (2016). Learn piano with BACh. CHI 2016. doi:10.1145/2858036.2858388
20. Shu et al. (2026). FretFlow. CHI 2026. doi:10.1145/3772318.3791540
