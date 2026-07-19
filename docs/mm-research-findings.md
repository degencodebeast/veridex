# Market-Maker research findings — the honest edge investigation

*Veridex Arena · TxLINE World Cup 2026 · research capstone for the MM strategy path*

> **One-line thesis:** We built the instruments that could *prove or kill* our own market-making
> edge story, ran them against the real Polymarket order book with cross-family adversarial review,
> and report exactly what they found — including the edges that aren't there. On a platform whose
> product is *honest agent scoring*, that discipline is the deliverable.

> **⚠️ EVIDENCE STATUS — `GATE_B_OPEN` (2026-07-11).** The findings in this document are **reviewed and
> reproduced in-session** (cross-family adversarial review), but they are **NOT yet independently
> re-verified** under the Research Verification Gate (Gate B), which is **OPEN**. Accordingly: **no finding
> here is `EVIDENCE_GATED` or `PROMOTED`**, none is claimed as independently verified research, and no
> profitability/alpha claim is made. Read every result below as *reviewed / scoped / pending independent
> re-backtest*, not as certified truth. The authoritative gate definition and the certification standard
> are tracked in the internal research-verification-gate spec. This banner is on-thesis: Veridex's own principle is "recompute from
> evidence; don't trust the agent that grades itself" — applied here to our own research.

---

## 1. Why this exists

Veridex Arena scores trading agents on a cryptographically-sealed **evidence ladder** — an agent's
claims are only as good as what can be independently verified. A market-making strategy is the
natural stress test: it makes many small, timing-sensitive decisions, so it is exactly where an
honest platform must be able to say *"this edge is real"* or *"this edge is not"* with evidence,
not narrative. This document is the research record for the MM strategy path: what we tested, how we
kept ourselves honest, and what the data actually said.

## 2. The instruments (and the honesty contract)

Two independent measurement instruments, each with a pre-declared honesty ceiling:

- **Instrument A — historical backtest.** The free public **pmxt** archive (tick-level Polymarket
  L2 order book: `book` snapshots + `price_change` deltas + `last_trade_price` prints + on-chain
  fill hashes) × **TxLINE** de-margined fair value (StablePrice) + confirmed goal/card events. Answers
  the **economics** question: make/take behaviour against the liquidity that actually existed.
- **Instrument B — single-clock live lead-lag monitor.** Stamps TxLINE FV arrival *and* venue-book
  arrival on **one local clock** — the only instrument that can resolve sub-2-second timing. Answers
  the **directional-lead** question.

**Pre-declared honesty ceilings (frozen before results):**
- **Cross-recorder ≠ single-clock.** pmxt's clock ≠ TxLINE's clock, so A reports **δ-swept economics**
  (act δ∈{0,2,5,10,30}s *after* the signal) and never claims "FV leads by X seconds." Positive-δ
  survival is *robustness*, not timing proof. Only B (one clock) can make a timing claim.
- **Counterfactual maker fills.** A maker fill is credited **only** when a real trade prints *through*
  the quoted price — no queue priority, size-capped — and is labelled COUNTERFACTUAL.
- **N = matches / goal-events, match-clustered.** Home/draw/away are mechanically linked responses to
  one event, never three independent samples.
- **Evidence ladder for fills:** `NO_POST_SIGNAL_STALE_QUOTE` → `QUOTED_STALE_NO_PRINT` →
  `STALE_PRINT_OBSERVED` → `OWN_FILL`. A stale quote is not a fill; a stale third-party print is
  stronger evidence but still not proof our order would win the race.

## 3. The directional / taker edge — NULL (convergent negative evidence)

Three tests, three ways, all pointing the same direction. *Convergent* negative evidence — not a
universal market law, but a decisive result for the tested TxLINE-follower taker family.

- **Spain–Belgium, single-clock (Instrument B).** Forward-predictive NEXT-change hit rate
  **0.498–0.499** (z≈0, 95% CI contains 0.5), with a correctly **anti-predictive placebo** (z≈−7.6
  to −8.1) proving the instrument works. The seductive "FV arrives ~2.5s before the venue, 100% of the
  time" is a **backward-recurrence artifact** (proven by recurrence symmetry), not information — the
  residual structure actually runs *venue→FV*.
- **France–Morocco, cross-recorder screen (Instrument A).** Same null at seconds scale.
- **France–Morocco, tick-level goal-shock (Instrument A).** At *both* real France goals the venue
  book fully repriced **~6.5 seconds *before*** the TxLINE goal signal, and **zero** of ~6,600
  post-signal trades printed at the stale pre-goal price → `NO_POST_SIGNAL_STALE_QUOTE`. The
  confirmed-goal signal tier lags by 73–111s — missing the ~1s reprice window by two orders of
  magnitude. Broadcast-latency sweepers reprice the book before TxLINE even reports the goal.

**Honest reading:** there is **no stale-quote taker edge for a TxLINE-follower**. The goal-shock
*phenomenon* is real and large (France-win 0.54→0.80 on the opener); the *exploitable lag* is not.

## 4. The maker (passive spread-capture) edge — NEGATIVE for every archive-testable variant

The taker nulls do not settle the **maker** question — a different estimand: does passive spread
capture survive *measured* adverse selection and fees? A bounded counterfactual-maker pilot on
France–Morocco tested it. Setup: quote symmetric around the venue mid (the *optimistic* reference — an
FV-centered quote would be staler and worse), continuously re-centered; credit a fill only when a real
trade prints against the quote; markout the venue mid over δ∈{1..30}s; net = half-spread − adverse
selection. **Triple-reviewed via independent cross-model adversarial review** — the conclusion was
**reviewed and reproduced in-session (NOT yet independently Gate-B-verified — Gate B is OPEN)**, and the two
implementation defects found in review were checked to *strengthen*, not flip, it.

**Result — negative at every tested depth:**
- The **conservative** measure — strict print-*through* (guaranteed fills, no queue priority) — is
  **more negative** than the inclusive headline: home-token h=0.005, δ=5s = **−0.041** (vs −0.035 once
  at-price, *queue-dependent* prints are included). Adverse selection ≈ **8× the half-spread**.
- **No tested *off-touch* half-spread breaks even** — but the grid starts at h=0.005, which is **~4× the
  real touch** (median venue spread ≈0.0025 → touch half-spread ≈0.00125). Standard touch-joining is
  *not* tested here.
- Near-goal fills are catastrophic (−0.09 to −0.21): the maker passively sells into the goal-rocket
  (238 short vs 55 long fills on the home token). Quiet-period fills (>90s from a goal) are
  **approximately break-even to mildly negative** (−0.008, CI spans 0) — and that split uses *future*
  goal knowledge, so it is a diagnostic, not an implementable policy.
- Fee stress only deepens it (breakeven fee is negative). Economics are equal-weight per qualifying
  print; size-weighting them leaves every cell negative. 95% block-bootstrap CIs are **within-match
  only** (in-sample stability; between-match variance untested at N=1).

**Mechanism — two adjacent facts + a bridge, NOT one engine** (the framing all three reviewers,
independently, corrected):
1. **Taker (a TxLINE-timing fact):** the venue reprices ~6.5s *before* the TxLINE goal signal → no
   stale-quote take edge.
2. **Maker (a *generic* fact):** a naive symmetric off-touch maker is dominated by **generic adverse
   selection** — aggressive venue flow runs through the resting quote — *independent of TxLINE's
   existence* (the tested maker never consults TxLINE).
3. **Bridge:** TxLINE is *also* too slow to *rescue* the maker defensively (its goal signal arrives
   ~6.5s after the sweep already filled you). This links the two but does **not** make TxLINE the
   *cause* of the maker's generic loss.

**What's ruled out vs. open (N=1, not "structurally dead"):** every maker variant *testable from this
archive* is negative — off-touch (all h≥0.005), pull-on-venue-momentum (−0.076 to −0.131; the venue's
own early move *is* the adverse selection, not an escape), and quiet-only. **Touch-joining with real
queue priority is UNRESOLVED** — the archive has no own-order queue position, cancellation-ahead, or
latency, and a touch-quote's apparent break-even is a *queue-capture illusion* (needs ~92% queue win on
the home token; the draw/away positivity is match-trend luck, zero TxLINE content). So the honest claim
is narrow: *the tested off-touch naive maker is negative on this fixture, and no archive-implementable
rescue works.* Touch/queue/inventory/pre-match/linked-hedge policies are scoped **future work**, not
overclaimed as dead. A preregistered ≥6-match extension (clustered by match) is the credible next step.

## 4.5 The broader strategy roster — one disclosed candidate, honestly labeled

Beyond make/take, the roster carries FV-only directional strategies (`CumulativeDriftStrategy`,
`MomentumStrategy`, `SharpMomentumStrategy`) and a `value_vs_venue` strategy. Three-family review
(independent cross-model adversarial review) mapped and adjudicated them:

- **`value_vs_venue` — FALSIFIED.** It compares TxLINE fair value against the venue price — i.e. it
  *is* the venue-timing bet. Our finding that the venue leads TxLINE makes its mids-dislocation stale
  by construction; its sealed "edge" was the `p·price−1` favorite-longshot artifact, not a measured win.
- **`drift` / `momentum` / `sharp` — a *different question*, and NOT invalidated by our venue-timing
  nulls** (they key only on the TxLINE StablePrice FV, never the venue). Drift's sealed run-001 result:
  `+61 bps` CLV overall, but the *broad* edge was correctly **REJECTED** (only the OU-totals family
  positive at `+162 bps`; 1X2 `−75`, AH `−25`). The surviving OU signal is:
  - **Circular-ish / same-source:** scored as CLV against TxLINE's *own* pre-kickoff close
    (`recompute.py:153`) — it measures "present TxLINE persists into future TxLINE," not independent alpha.
  - **NOT a naive momentum artifact** — a cheap autocorrelation probe found the feed *mean-reverts*
    tick-to-tick (OU lag-1 = −0.10, z=−8.8), refuting the "smoothing induces momentum" explanation.
  - **But selection-driven and unproven:** the OU edge is carried entirely by drift's trend-selection
    gate on a *mean-reverting* base, over **n≈18 fixture clusters** with **post-hoc family selection** —
    the shape of a post-selection artifact, indistinguishable from a real trend-catcher without
    out-of-sample data — and the first genuinely out-of-sample (OOS) test (frozen OU-only, N≈4 predeclared →
    **N=2 finished**) did **not** reproduce it: *falsifying* on the circular metric (+162 → −254 bps
    clustered), **NULL on independent settled outcomes** (≈0), confirming nothing either way at N=2.
  - **And not monetizable even if real:** drift is a pre-match/minutes signal, and the pre-match venue
    is *frozen* (§7) → no execution surface.

**Honest label (retained for judges):** *candidate rung-1 CLV signal, not proven executable alpha* — the
predeclared out-of-sample promotion protocol (OU-only frozen policy, genuinely-new fixtures,
independent tradeable close + settled-outcome calibration, fixture-clustered) was **run and did not
promote it** (N=2 finished; a genuinely-new N≥4 extension remains future work). This
is the platform thesis again: a positive-looking in-sample number, disclosed with exactly what it is and
isn't, rather than sold as alpha.

## 5. The methodology story — self-consistency is not truth

The single most instructive episode of this investigation was a bug we caught in our *own* pipeline:

- Our confirmed-goal extractor reported **France 2–1 Morocco.** The truth is **2–0.** A provisional
  Morocco goal was retracted 1.7s later (via a `Goals`-key deletion the extractor swallowed), and the
  market never believed it (Morocco-win priced at 11% while supposedly leading).
- It passed our unit tests, a full code review, *and* an internal consistency invariant — because the
  invariant was **circular** (it checked the extractor against a figure derived the same buggy way).
- It was caught only by an **independent model family** checking *orthogonal* evidence — the official
  `game_finalised` record and the market's own prices.

The fix replaced circular self-validation with an **external settlement gate**: "confirmed" now means
*the feed agrees with an external final score*, and any fixture without external truth is labelled
`unvalidated`, never silently confirmed. This is the platform thesis in miniature — **an internally
consistent claim can be wrong; only an external anchor makes it evidence.**

## 6. How we stayed honest (the review discipline)

Every load-bearing claim in this document was subjected to **cross-family adversarial review** — two
distinct model families (GPT-family and a separate family) independently re-deriving results from raw
data, specifically prompted to *refute*, not ratify. Agreement between different families is genuine
corroboration; agreement within one is an echo. This process repeatedly caught real errors the primary
analysis got wrong (a mis-attributed mechanism, a NaN-poisoning path, and the phantom goal) — each of
which, unverified, would have shipped a subtly-wrong result.

## 7. Honest limitations (the ceiling, stated as a strength)

- Cross-recorder clocks: A screens and *falsifies* timing candidates; it cannot *prove* sub-second
  capture. Single-clock proof requires Instrument B (or a live guarded order).
- N is small (1 fixture / 2 goals for the tick pilot; 2 matches for the directional null). The nulls
  are convergent and decisive for the tested family, not a claim about all markets or all strategies.
- Maker fills are counterfactual executability, not proof of queue priority or real fills.
- Goal-derived results beyond France–Morocco are **exploratory/unvalidated** pending the pipeline
  hardening tracked in the internal research backlog.
- **The "venue leads TxLINE ~6.5s" finding is IN-PLAY-ONLY** — measured at in-play goal events in
  liquid WC-soccer matches. It is *not* a universal claim. The mechanism (Polymarket's fast informed
  in-play sweeper flow front-runs TxLINE's confirmation pipeline) is specific to live, liquid,
  event-driven moments. Other in-play taker angles (cards, non-goal FV jumps) share this mechanism and
  are expected null.
- **Pre-match TESTED → NULL / underpowered (the venue is FROZEN, not lagging).** The mechanism predicts
  the opposite where the venue is thin, so we tested it across the played WC corpus, [KO−60m, KO−2m]:
  does TxLINE FV lead the pre-match venue mid? The pre-match Polymarket book barely quotes — a *median
  of ~2 venue mid changes per pre-match hour*, ~16× fewer than FV changes — so there are almost no venue
  moves to lead. Both families raw-verified the freeze against the un-downsampled `price_change` stream
  (Netherlands–Morocco 61k raw rows → **0** top-of-book changes; the same token in-play had 156k) — it is
  **genuinely frozen, not a downsampling artifact**. Corrected accounting (both reviews): **17 matches
  with data** (Turkey–USA was dropped by a filename mismatch; South Africa–Canada is zero-*data* not
  zero-*moves*), the "67" total is a 1s-bucketed count (true ~150–175, still ~16× below FV), and the raw
  probe reproduces null (corrected 50 bps: 9/20 = 0.450, z=−0.45; valid anti-predictive placebo z≈−5.5).
  So the venue is either **FAST** (in-play) or **FROZEN** (pre-match); **TxLINE does not demonstrably
  lead a *moving* venue anywhere tested.** Honest scope (both families): this **closes the angle for the
  submission — it is NOT a universal statistical rejection** (the corpus is underpowered, ≤4 testable
  matches). The *stale-level catch-up* variant is also dead on
  data: at KO−2m the frozen mid already sits within 3–67 bps of FV (no stale level to catch up from).
- **TxLINE's structural value is NOT speed** — it is the *sharp de-margined fair-value anchor* (breadth)
  + *cryptographic provenance* (provable "when you knew it"), which is the right primitive for honest
  agent scoring independent of any latency race.

## 8. What this delivers

1. A **backtestable** MM strategy path instrumented against the real order book, with pre-declared
   honesty ceilings.
2. An **autonomous** make/take decision surface driven by provable fair-value + event signals.
3. An **honest, precisely-scoped result**: on the tested fixtures, (a) the directional/taker edge is
   null — the venue reprices ~6.5s *before* the TxLINE goal signal (a TxLINE-timing fact); and (b) a
   naive off-touch passive-maker is negative — dominated by *generic* adverse selection, independent
   of TxLINE; with the bridge that (c) TxLINE is *also* too slow to defensively rescue the maker.
   These are two adjacent findings plus a bridge, **not** one TxLINE-driven mechanism (a distinction
   all three reviewers required). The market is efficient at seconds scale *relative to the TxLINE
   signal* in these in-play liquid events — not proven market-wide (N=1–2). The research discipline
   that produced this (external-truth validation, three-model / two-family adversarial review, self-caught bugs)
   is itself the evidence that Veridex's honest-scoring thesis is real, not marketing. (This summary
   predates §§4.6–4.11 below, which were appended later and extend it: smarter/asymmetric makers were
   subsequently tested — §4.6, none positive — and the maker null was replicated at N=6 — §4.10.
   Genuinely-untested and honestly out of scope: touch-joining with real queue economics, other
   liquidity regimes, other FV providers, and other sports/competitions — scoped as future work, not
   overclaimed.)

## 4.6 Smarter-maker variants — all tested, none positive (the maker question, closed for the tested off-touch counterfactual policies)
Our reviewers flagged the "smarter maker" (inventory-skew / quiet-period-only / event quote-pull) as the
one untested maker variant beyond the naive baseline. Tested on France–Morocco (N=1, exploratory, STRICT
print-through, mid-anchored + zero-fee = generous to the maker):
- **Naive baseline:** net@δ5s −0.048 (CI below 0) — reproduces the pilot.
- **Inventory-skew:** −0.030 (CI below 0) — a damage-limiter (halves the bleed, collapses max inventory),
  NOT an edge.
- **Quiet-period-only:** −0.016 (CI [−0.027,+0.002]) — **breakeven, not positive** (no alpha in quiet).
- **Event quote-pull on the real TxLINE signal:** −0.053 — **WORSE than doing nothing.**
- **The crux:** 100% of toxic fills print BEFORE the TxLINE goal signal exists (Goal 1: 33/33 pre-signal
  shorts at −0.203 markout; only 7 benign post-signal fills). A ~6.5s-late signal can't avoid a fill that
  already happened — quote-pull removes benign post-goal fills while keeping the toxic pre-goal sweep.
  Cost of the 6.5s lag ≈ 0.039/share (TxLINE-pull vs unachievable oracle-pull).
**Verdict:** no implementable smarter-maker variant turns positive; the best they do is claw back toward
breakeven by NOT trading around goals — damage avoidance, not edge. Notably, the marquee "pull quotes
around goals" mechanism **backfires once its ~6.5s signal latency is honestly modeled** — the toxic fills
have already happened before the goal signal exists.

## 4.7 Cross-market consistency (Dixon-Coles / cross-market relative value) — NULL
Test: are TxLINE's 1X2 / OU / AH families (all StablePrice) internally inconsistent with one coherent
goal model — a mispricing INSIDE TxLINE, independent of the venue-leads thesis? N=22 fixtures, pre-match
FT snapshots, de-margined probs verified summing to 1.0 (0/292 violations).
- The naive "1X2 → OU/AH" gap of 455 bps is a MODEL ARTIFACT (1X2 barely pins total goals; independent
  Poisson mis-links 1X2→total).
- Fitting ONE coherent goal model jointly across all three families → **~90 bps RMS** (Dixon-Coles),
  and the residual is **non-systematic** (mean signed gap −17.5 bps; 14/15 pooled CIs straddle 0; the one
  marginal AH −0.5 fails Bonferroni). ~80% of the apparent inconsistency dissolves once the total floats.
- **Verdict: no SYSTEMATIC cross-market inconsistency detected** under the tested Dixon-Coles model + corpus; the residual is
  within plain-model misspecification. And **no execution surface** (WC Polymarket lists only 1X2, no
  OU/AH) → signal-only even if a residual existed.

## 4.8 Cross-venue convergence (does the venue move toward the TxLINE fair?) — SUGGESTIVE-POSITIVE, not tradeable
> **(2026-07-11) Contamination fix + independent verification.** This probe's original NULL used a hand-rolled
> goal detector blind to lifecycle retractions (the phantom-goal bug). Re-running through the fixed extractor
> **flipped the sign**, and an independent adversarial self-check **reproduced** it: corrected goals
> (FRA-MAR 2-0), the goal-excluded 5-min net edge is **+1.86pp [+0.70,+3.10]** (grid-robust: 32/36 cells net>0,
> 25/36 CI>0; seed- and leave-one-out-stable), and Findings 2 & 4 below (the "FV rides own reversion" and "the
> edge is goal-capture" attributions) were phantom-goal artifacts — **retracted**. **BUT the honest label is
> *suggestive-positive, underpowered, NOT tradeable as tested*:** the cleanest orthogonalized FV-specific test
> still **spans 0** (+1.35 [−1.56,+3.63]), on N=19 single-tournament clusters, counterfactual fills, a swept
> grid with ~10× overlapping entries, and no OOS. It is a **candidate for R4-B**, not a proven edge, and the
> full methodology re-backtest (Gate B) remains open, so it is NOT promoted. (Distinguish the FM ~6.5s pilot
> from the scaled −5.18s median.)
> **⟨SUPERSEDED ORIGINAL RESULT — pre-fix, retained for provenance. The banner ABOVE is the authoritative
> corrected verdict. The numbers in the four bullets below (sign-match 0.704, FV-specific t=1.17,
> "mostly GOAL-capture", the INCONCLUSIVE/NEGATIVE verdict, "no tradeable path") were computed on the
> phantom-goal-CONTAMINATED goal set and are RETRACTED — Findings 2 & 4 here are the exact ones the lifecycle
> fix overturned. Do NOT read the block below as current.⟩**

This is the venue-EXECUTABILITY question for the tested TxLINE-FV directional signals: when Polymarket is priced
away from the TxLINE de-vig fair, does the venue mid subsequently CONVERGE toward it (a tradeable
divergence)? In-play non-goal regime, 19 fixtures, fixture-clustered, cross-recorder (δ-swept).
- **Naive read looks like a win** (sign-match 0.704, across-fixture t=4.44, beats placebo 0.423 + AR
  control) — but it does NOT survive the correct control.
- **Under a 20s own-reversion control** (cluster-robust OLS): the venue's OWN short-window reversion
  dominates (t=2.95); the **FV-specific increment is n.s. (t=1.17)** — the FV gap is collinear with the
  venue reverting its own microstructure noise; FV just marks the level it was heading to anyway.
- **Placebo-nulled but dies at the spread:** the tradeable edge is mostly GOAL-capture (buying a team
  cheap that then scored, not convergence to fair). Excluding goal-window exits: 300s +2.53pp → +0.46pp
  (CI spans 0); at 60s ~0pp vs a 0.91pp round-trip spread.
- **Verdict: INCONCLUSIVE/NEGATIVE** — the venue direction toward fair is not refuted, but the
  FV-specific effect is statistically indistinguishable from zero at N=fixtures, and net of spread it is
  NOT an edge. → **No tradeable venue-executability path for TxLINE-FV directional signals** (drift /
  momentum / sharp): the venue doesn't reliably converge to the TxLINE fair beyond its own noise.

---
## Research status: COMPLETE for the frozen Polymarket / WC hypothesis set. Every make/take/directional
## angle in the pre-declared set was tested; no positive, tradeable TxLINE edge found on this corpus; each
## null honestly explained. The credibility deliverable is the rigor, not an alpha claim.

## 4.9 SharpMomentum v2 characterization — under-fires pre-match (regime effect), CLV null
The one roster strategy never characterized (robust-z + Page-Hinkley + persistence; "barely fires").
- **Pre-match (the scored window): under-fires** — 36 fires / 22 fixtures (10 never fire), only 15
  close-scoreable, ALL totals, zero 1X2. The robust-z≥2.5 gate blocks 99.97% of pre-match rising samples
  — because pre-match TxLINE FV is quiet (nothing sharp to detect before kickoff). NOT a broken detector.
- **Full stream: fires abundantly (923×) but 95.7% IN-RUNNING** — real sharp moves (goals/events) happen
  in-play. So the strategy's active regime is IN-PLAY.
- **The structural connection:** SharpMomentum fires exactly where TxLINE LAGS the venue (~6.5s, in-play)
  and stays quiet exactly where the venue is FROZEN (pre-match). Its active regime is precisely where
  TxLINE has no timing edge — the same structural fact as every other null, via a different strategy.
- **Circular CLV of its 15 scoreable fires:** fixture-clustered −87 bps, CI [−282,+108] (straddles 0);
  pooled↔clustered sign-flip = pseudo-replication. NULL / uninterpretable; circular + not venue-tradeable
  regardless. The probe refused to p-hack (did NOT
  loosen thresholds to force fires).

**The pre-declared roster is now fully characterized for the frozen Polymarket / WC hypothesis set.** Every
strategy (drift, momentum v1, sharp-momentum v2, value-vs-venue) + every angle (taker, maker naive+smart,
pre-match, cross-market, cross-venue) in that set was tested; no positive tradeable TxLINE edge on this
corpus; each null structurally explained by the same core fact — the venue is fast (in-play) or frozen
(pre-match), and TxLINE never leads a *moving* venue. (Honestly out of scope, not claimed closed:
touch/queue-aware making, other FV providers, other sports/competitions, and other liquidity regimes.)

## 4.10 Maker null SCALED — N=1 → N=6 cross-match (the robustness the pilot lacked)
The FM maker pilot was N=1 (within-match bootstrap CI only — reviewers' key limitation). Extended to a
FROZEN, pre-declared 6-match set (FM, ESP-BEL, + 4 more; goal counts matched the pre-declaration exactly
→ freeze held), fixture-clustered, 6,056 STRICT print-through fills:
- **Maker-negative replicates 6/6 — ZERO matches positive.** Per-fixture net@δ5s h=0.005 ∈ [−0.023,−0.012];
  between-fixture SD tiny (0.0047 ≪ the −0.018 level).
- **Cross-match 95% CI = [−0.023, −0.013]** (t, df=5) and [−0.021,−0.013] (fixture bootstrap) — **entirely
  below zero.** Contrast the N=1 pilot CI [−0.078, +0.0004] which *touched* breakeven: at N=6 the interval
  is TIGHTER and MORE decisively negative. The null is a low-variance cross-match phenomenon, not a fluke.
- **Correction to the pilot's optimistic read:** quiet-period fills are NOT breakeven — pooled −0.0083,
  negative in 5/6 matches. A goal-avoiding maker is loss-MINIMIZING, not a positive edge. Near-goal (−0.034)
  is ~4× the quiet loss; the pattern holds match-to-match.
- Every (h × δ) cell is 6/6 negative; wider spread = worse; breakeven fee is negative (no fee ≥0 works).
- **Limitation:** N=6, same-venue/same-competition (cross-MATCH, not cross-regime); counterfactual fills;
  5/6 goal-sets manifest-unvalidated (counts match pre-declaration, only FM settlement-verified).
**Verdict: the maker null HOLDS and STRENGTHENS at N=6.**

## 4.11 Taker null SCALED — N=2 goals → 48 extracted goals, 35 clean / 16 fixtures analyzed (robust, with one honest exception)
Scales §3's FM taker null across the played slate (updates §3's "convergent negative" to a
fixture-clustered result). Harness reproduces FM to the second; 11 provisional phantoms retracted
slate-wide; goalless controls → 0 (placebo passes).
- **Null ROBUST:** clean pool n=35 goals / 16 fixtures; fixture-clustered median Δ(venue onset − TxLINE
  signal) = **−5.18s**, 95% CI **[−6.06, −4.28]s** (excludes 0); **83% NO_POST_SIGNAL_STALE_QUOTE**;
  15/16 fixtures venue-led; **FM mid-pack (−5.14s) → not cherry-picked.**
- **THE HONEST EXCEPTION (characterized, not hidden):** Belgium–Senegal inverts it — venue LAGGED +6s
  median, one 88' goal with a genuine ~8s stale-quote window — BUT on a thin/late/low-probability
  (~1–10¢) token → not a scalable venue-wide edge.
- **The correct honest claim:** the venue leads on the vast majority of goals and 15/16 fixtures; a
  durable stale-quote taker window is the RARE EXCEPTION seen once — consistent with a *candidate* thin/late
  low-liquidity regime (one counterexample suggests the hypothesis; it does not prove the regime), not a
  repeatable edge.
