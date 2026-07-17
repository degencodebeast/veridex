# Research Journey — A Decision Log

*How a non-expert built a provable trading arena by making every strategy call auditable*

I came into this with **zero prior market-making experience and zero prior algorithmic-betting experience.** I say that up front because it is the frame for everything below. I could not lean on intuition I did not have, so I did the only thing that was honest: I made every strategy decision **research-directed**. Each call in this project is a small chain — *hypothesis → the test I ran → the verdict → the decision that followed* — and I kept the whole chain because I did not trust myself to remember which conclusions were earned and which were just hopes.

That turned out to be the point. The track rewards clean, deterministic, strategically defensible logic. A careful non-expert who shows the reasoning is, I think, *more* credible than an expert who says "I just know" — because you can check my work. This document is that check. It is a log of what I believed, what I tested, what died, and what survived.

The most important section is the **strategy graveyard**: the ideas I killed. I am proud of that section, not embarrassed by it, because the discipline to *not build* something the evidence did not support is the discipline the whole platform is built to enforce.

> **⚠️ STATUS UPDATE (2026-07-11) — read this before the "flagship" framing below.** Where this log
> describes the **FV-lead directional taker** as "the flagship live lane," that reflects the decision
> *at the time*. It has since been **tested and found NULL** — the venue reprices ~6.5s *before* the
> TxLINE signal, so there is no stale-quote taker edge (and the naive/smarter maker is negative
> across N=6 matches). The FV-lead lane is now itself a **graveyard entry**, not a live edge. The
> authoritative, current record is `docs/mm-research-findings.md`. This journey is preserved as the
> honest *narrative of how the conclusion was reached* — the "flagship" language marks a hypothesis I
> pursued and then killed, exactly the discipline this document is about.
>
> **Two live updates (2026-07-11):** (1) the goal-dependent nulls (taker at goals, maker) are under a
> *phantom-goal re-verification* — a lifecycle-retraction bug was found in one probe's hand-rolled goal
> detection and every goal-dependent result is being re-checked through the fixed extractor; treat their
> exact magnitudes as provisional until that lands. (2) The **cross-venue convergence** result (whether
> the venue moves toward the TxLINE fair) *flipped* once its goal set was corrected — from a null to a
> **suggestive-positive but underpowered, not-tradeable-as-tested** candidate now under independent
> verification. Neither changes the headline honesty (no *proven* tradeable edge); both are tracked in
> `docs/mm-research-findings.md`.

---

## 1. Premise, and how my thesis evolved

The raw material I was handed is unusual: TxLINE publishes a **de-margined StablePrice** — a sportsbook-consensus probability with the bookmaker's vig removed. My first instinct was the naive one: *if I have a good fair-value probability, I can predict matches.* I killed that framing almost immediately, and it is worth saying why, because it shaped everything after.

**I decided I would not claim to predict soccer better than the market.** I have no model that beats a de-vigged consensus of professional bookmakers at forecasting a World Cup fixture, and pretending otherwise would have been the first lie. So I reframed the entire thesis:

> **This is not match prediction. It is price dislocation.**

The consensus fair value is a *reference price*. The opportunity, if one exists, is not "I know the true probability better than everyone" — it is "the fair value and some venue's tradeable price disagree, and I can act on that gap before it closes." That is a much smaller, much more defensible claim, and it is falsifiable.

From there the thesis walked through four stages, each one a response to what the previous stage taught me:

1. **Fair-value dislocation** — TxLINE fair value vs. an executable venue price. The intuitive form is a probability gap; the form that actually decides a trade is expected return after costs. I kept them as separate quantities on purpose (a probability gap is *not* an executable edge), because conflating them is how people fool themselves into "profitable" backtests.
2. **Stale-line / latency** — if the fair value *moves first* and a venue has not repriced, the gap is a timing edge, not a disagreement about truth. This is the version of the thesis that is actually tradeable, if it is real.
3. **Market-making** — quote around fair value, earn the spread, let the timing edge protect the inventory.
4. **FV-lead directional taker** — where I landed. My research indicated that a *lead signal is structurally a taker expression, not a maker edge* (I explain this pivot in §2), so the flagship live lane became: cross a stale quote when the fair value has demonstrably led it, and only then.

I am showing the whole arc so the pivots read as what they were — *learning* — and not as indecision. Each move was forced by evidence, and each one made the claim narrower and more honest.

---

## 2. The strategy graveyard

This is the centerpiece. Four lanes I tested and either killed or deliberately deferred. Every one of them was a real piece of work that produced a real verdict, and the verdict was "don't build the thing you wanted to build."

### 2.1 The event-window fork probe → INCONCLUSIVE (as sealed; corrected 2026-07-11 to a weak FADE) → I built neither follower agent

**Hypothesis:** after an in-play event (a goal, a red card), does the venue *lag* the fair value (build a lag-follower agent) or *overreact* to it (build a fade agent)? These are opposite trades, and the data was supposed to fork me toward one.

**Test:** I ran an event-window fork probe over the sealed fixtures, aligning fair-value moves to venue moves around events.

**Verdict:** it sealed **INCONCLUSIVE** *(as sealed, 2026-07-06)*. **⚠️ [CORRECTED 2026-07-11:** that sealed v2 used a goal extractor blind to lifecycle retractions — the phantom-goal bug. Re-run through the fixed extractor, the probe flips to a **weak `FADE`** signal (median-ratio 90% CI [1.0070, 1.0564], just above 1). The sealed v2 is preserved byte-for-byte and marked `SUPERSEDED_BY_PROTOCOL_DEFECT`; a corrected v3 is pending operator + external review.**]**

**Decision:** I built **neither** the `EventLagFollowerAgent` nor the `EventFadeAgent`. This is the graveyard entry I am most deliberate about: an inconclusive probe is not a weak yes. It is a no-build. Shipping a follower *or* a fader off a coin-toss probe would have been a strategy justified by nothing, and the arena would have ranked it as if it meant something. *(Post-correction 2026-07-11 the verdict is a **weak fade** rather than a coin-toss — but still underpowered, same-sample, and unverified: I built neither agent, and the corrected v3 must clear the Research Verification Gate before any fade agent could be justified. The discipline holds; the honest update is "weak fade signal, not built," not "inconclusive.")*

### 2.2 The Polymarket executable-edge probe (Run-002) → FALSIFIED

**Hypothesis:** the candidate CLV signal from the drift agent (see §3) translates into a real executable edge against Polymarket prices.

**Test:** I priced the drift agent's in-scope 1X2 decisions against time-aligned Polymarket mids, under a bounded-staleness rule, with a pinned coverage hash so the sample could not be quietly reshaped after the fact. The venue lane *worked* — it priced **94.7%** of the in-scope decisions.

**Verdict:** the result was a near-perfect **monotonic longshot ramp** — roughly **+607 bps** of apparent edge at 0–20% probability decaying smoothly to **+33 bps** at 80–100%. That shape is the fingerprint of a favorite-longshot / de-margin-scale divergence — a *structural* artifact of how two different probability scales disagree at the tails — **not** strategy-specific dislocation. `real_executable_edge_bps` was `None`; these were estimated mids, not fills.

**Decision:** I labelled it a structural divergence to **falsify**, not alpha to trade. It would have been trivial to headline "+607 bps." The platform surfaced the ramp and I killed the clean-edge read. The honest follow-up (all-outcome normalization, depth instead of mids, a bigger sample) is predeclared as future work, not retrofitted after a nice number.

### 2.3 The polled ~2-second live monitor → NO confirmed sub-2s lead → a coin-flip null

This is the lane where I was most tempted, and where the data was most disciplining.

**Hypothesis:** the offline finding was encouraging. A committed lead-lag probe (`scripts/maker/leadlag_probe.py`, commit `524d27b`) showed, on a **backfilled** venue series, that TxLINE fair value *led* the Polymarket mid: **NEXT-change 0.640 @50bps, fixture-level z ≈ +4.12, 17 of 18 fixtures, placebo correctly anti-predictive.** If that lead was real on *live* quotes, it was the edge.

**The blocker I named before testing:** the backfill had a ~20–40 minute cadence. That could be an artifact of the historical price endpoint, not a property of the live market. A lead measured against a slow, stale series can evaporate against fast live quotes. So I built a read-only live monitor to check.

**Test:** five live sessions across the World Cup quarterfinals, 12 markets each, ~2-second polling. The primary session (mainnet, ~39,700 rows, ~110 minutes) was the substantive one.

**Verdict — two questions settled, one left open:**
- **Cadence:** the live venue moves on a **seconds** cadence — median ~4s between changes (p25 2s, p75 9s), not 20–40 minutes. The slow-venue-staleness edge **does not transfer**; the backfill cadence was a sampling artifact, exactly the risk I had flagged.
- **Lead:** in the high-n primary session, **next-change sat at 0.507** — a coin flip. 95% CI [0.475, 0.539] contains 0.5; z ≈ +0.45; n = 954 scored events. Two *small* sessions showed an apparent lead (0.73–0.75), but it **failed to replicate** in the session with 17× more events on the same fixture, so under "largest clean out-of-sample wins" discipline I could not claim it. The placebo was correctly anti-predictive throughout (primary 0.42, z ≈ −4.89), so this was a real null, not a broken pipeline.
- A useful side result: **mainnet and devnet agreed**, which refuted an assumption I had been carrying that devnet was 60s-delayed and therefore a different experiment.

**Decision:** I did **not** promote a live FV-lead edge. I sat on the ~0.507 null honestly. But the monitor was structurally incapable of resolving one thing: a *sub-2-second* lead is invisible to a ~2-second poll when the venue itself moves every ~4 seconds. So this probe **settled the cadence question and the "is there an obvious lead" question, and left the sub-2s question genuinely open** — which is what motivated the tick-resolution monitor in §3.

### 2.4 Maker-first → deferred in favor of taker-first

**Hypothesis (the one I started with):** the natural home for a fair-value edge is market-making — quote around fair value and earn the spread.

**What my research indicated:** a *lead signal is structurally a taker expression.* If my only evidence is "fair value tends to move before the venue," the clean way to express that is to **cross** a stale quote in the direction the fair value has already moved — a taker action. A maker edge is a different and harder claim: it requires evidence about toxicity, inventory origin, queue position, and cancel latency, none of which an FV-lead finding provides. And there is a subtler data problem: **Polymarket's own API and the chain don't serve a historical order book** — only fills and mids — so a *precise* maker-fill backtest, which needs per-order queue position, isn't available from the venue itself. (Third-party archives of the live feed do provide historical L2 *depth*, enough for an *approximate* maker backtest; what they can't reconstruct is exact per-order queue position, because the feed is aggregated by price level.)

**Decision:** I deferred maker-first and made the first live alpha lane a **FV-lead directional taker.** FV-lead alone does not prove a maker edge, so I refused to build the arena around one. Maker becomes a separate, later, harder-gated branch — if the evidence ever supports it. **[SUPERSEDED 2026-07-11 — this decision is now historical: the FV-lead taker was subsequently tested and found NULL (scaled), so it is a graveyard entry. The current R4-B lane is the venue-anchored, TxLINE-guarded `EXPERIMENTAL_DUST` maker (see the status banner at the top of this doc and `.omc/plans/r4b-strategy-design.md`).]**

---

## 3. What survived, and why

What survived is a stack of **diagnostic and infrastructure lanes**, not a claimed edge. I want to be precise about that: the thing I built well is the *honesty machinery*, and I built it on purpose, as a set of design decisions each with a reason.

**The market-making evidence ladder (R1 → R4-B).**
- **R1 — quote-quality markout.** Measure how a quote does against where the market goes next. The most basic "is this quote any good" diagnostic, report-only.
- **R1.5 — trade-aware diagnostics.** Where historical trades can be joined to markets by `condition_id` / `token_id`, enrich the markout with what actually traded.
- **R2 — report-only sensitivity / fee-stress.** Vary the assumptions adversarially — a 4× fee haircut, a taker-fee floor, quote-distance and stop sweeps, boundary regimes near 0.05/0.95 — and *report* whether the edge survives. Never ranked, never a fill claim. The design rule I adopted: making only clears when `spread > adverse-selection + taker-fee`, which is a high bar that tilts the honest answer toward *taking or not quoting at all.*
- **R3 — the live recorder.** A separate, isolated lane (`veridex/live_recorder/`) that records the live L2 book, fair value, decisions, quote intents, and latency — sealed and replayable. This is where the sub-2s question moves: R3 also **measures executability** — was there fillable size at a cost-clearing price at the moment the fair value diverged? — which is the bridge from "a lead exists" to "it was actually tradeable."
- **R4-A — dust execution safety.** The order lifecycle done carefully: isolated funded wallet, loss caps folded into the policy hash, breaker/kill-switch → cancel-all, startup/shutdown open-order sweep, reconcile-before-retry after an uncertain ACK. This layer answers *"can we trade safely?"* — not *"should we?"*
- **R4-B — the strategy / experimental-policy layer.** *(Updated 2026-07-11: the original "FV-lead taker policy" is **superseded** — that lane was tested and found NULL.)* The current R4-B candidate is a **venue-anchored, TxLINE-guarded `EXPERIMENTAL_DUST` maker**: the venue book anchors the quote, while TxLINE (an independent reference + live match state) deterministically gates whether the agent quotes, skews, widens, or abstains. It carries a `NOT_PROVEN_EDGE` ceiling — a bounded, honestly-labeled experiment, not a proven edge — and answers *"should we trade this signal, and how?"* as a deterministic, recomputable gate over the tape, **not** an LLM inference. (Historical: the first-drafted R4-B was an FV-lead directional taker; see §2.4 and the top-of-file banner.)

**The honesty infrastructure — deliberate design choices, not decoration.**
- **A sealed, append-only evidence ladder.** Every recorded event is immutable (`frozen`, `extra="forbid"`), so no fill/PnL/edge field can be smuggled onto an event after the fact — the schema rejects it at construction. The session seals to a content hash, so a crash-partial replay is still verifiable.
- **Two-dimensional no-look-ahead alignment.** Fair value and venue observations are aligned so a decision can only ever see information that existed at decision time. This is the single most important guard against the classic backtest lie.
- **Counterfactual-only executability.** The executability measurement is pinned to the literal label `"COUNTERFACTUAL"` and has *no* `fill_price` / `filled_size` / `realized_pnl` fields. It records what it *would* have cost to clear the book — never a claimed fill. The type system enforces the honesty.
- **Structural rank-isolation.** A denylist and structural import tests guarantee that no fill, PnL, or inventory value can ever leak into scoring, the rank key, or the leaderboard. The trust path is import-audited to contain zero LLM SDK code. The agent proposes; it cannot grade itself.
- **The sub-2s dual-recv-timestamp WS monitor.** Because the polled monitor could not see a sub-2-second lead, I built a WebSocket, tick-resolution monitor that stamps the fair-value receive time and the venue receive time on the *same local clock*. This is the instrument purpose-built to answer the one question §2.3 left open. **[UPDATED 2026-07-11 — ANSWERED *for the tested fixture*: a single-clock NULL, not a general or market-wide result.]** The Spain–Belgium live single-clock WS session (fixture 18218149, ~149 min, mainnet+devnet, n≈8,000 scored venue changes at tick resolution) *is* the sub-2s dual-recv test this instrument was built for, and it returned a **NULL**: forward-predictive NEXT-change **0.498–0.499** (95% CI contains 0.5, z≈0), with a correctly anti-predictive placebo (z≈−7.6 to −8.1) proving the wiring — and the seductive "2.5s, ~100% FV-first" arrival lead was proven to be a **backward-recurrence artifact**, not information (residual structure actually runs venue→FV). Doubly-derived (analyst + independent Fable re-derivation from the raw tape). Detail: `.omc/research/spain-belgium-live-probe.md`; capstone `docs/mm-research-findings.md` §3.

One more survivor belongs here for context, because it is the "hook" number and it needs its honesty attached. A drift-template agent (`CumulativeDriftAgent`) averaged **+61.19 bps CLV** across the 18-fixture sample and beat all three deterministic baselines on identical sealed inputs (favorite +4.6, threshold-move −126.5, seeded-random −341.7 bps), net-positive on 10 of 18. That is a **candidate rung-1 CLV signal, not proven executable alpha** — CLV is measured against the sharp TxLINE close with no venue leg, effective n ≈ 18. And when we ran the honest promotion test — the surviving OU-totals slice, frozen and taken **out-of-sample (OOS)** — it did not survive: *falsifying* on its own metric, **NULL on independent settled outcomes**, at **N=2 effective fixtures** — so it stays a benchmark, never promoted as a real edge. What makes it credible is not the number's size but its provenance: it was recomputed by the law, never self-reported, and its venue translation (§2.2) was *falsified*, not sold.

---

## 4. My decision points — where the judgment was mine

Tools and AI reviewers were instruments in this project. The judgment was mine, and these are the places it shows.

- **I chose a real small-money live path over the safer simulated / devnet path.** The track would have accepted a clean simulation. I wanted to genuinely learn live execution — the reconciliation, the uncertain ACKs, the kill-switch behavior against a funded wallet — because a simulation cannot teach you what a real order lifecycle does. So R4-A is built as if the money is real, because the whole point is that it eventually is.
- **The taker-over-maker pivot** (§2.4) was my call, made against my own starting instinct, because the evidence said a lead signal is a taker edge and I would not build the arena around a maker edge I had not proven.
- **The depth bet.** I chose to build honest infrastructure — the sealed ladder, the counterfactual executability, the rank isolation — for an edge I had *not yet proven*. That is a bet that the *provability* is the durable asset even if the specific edge dies. I made it deliberately, knowing a flashier path was to skip the machinery and headline a number.
- **I ran an independent blind cross-check panel** instead of trusting a single reviewer. For the execution-safety architecture I had three neutral reviewers evaluate the design *blind* — no exposure to each other or to the primary review — and only counted a finding as solid when it survived independently. That panel caught concrete code-level gaps (a `cancel_order` that actually cancelled *everything*; loss caps missing from the policy hash; a diagnostic field that could leak into rank) that pure architecture reasoning had missed.
- **The epistemic discipline of not over-counting agreement.** When AI reviewers agree, that agreement is worth less than it looks if the reviewers are correlated. I deliberately weighted *model-agnostic test evidence* — a red test that drives a guard with realized inputs — over reviewer consensus. Three models agreeing from the same prose is not three pieces of evidence; a guard firing against a recording fake is one real one. That distinction is a judgment call, and it is the one I am most confident was correct.

---

## 5. What is still open, and unproven

I would rather end on honest open questions than a false bow. Stated plainly:

- **Whether a genuine sub-2-second FV lead exists — TESTED → NULL (updated 2026-07-11).** The polled monitor returned a coin-flip null at 2-second resolution and was structurally blind below it. The tick-resolution WS monitor was built to answer this, and it has now run on live match evidence: the **Spain–Belgium single-clock WS session** (the same dual-recv instrument, n≈8,000 tick-resolution scored changes) returned **0.498–0.499** — a null, CI containing 0.5 — with a valid anti-predictive placebo, and showed the apparent "2.5s FV-first" arrival lead is a backward-recurrence artifact (residual runs venue→FV). So there is **no confirmed sub-2s FV lead** on the tested live fixture. This is a single-fixture live result (not a market-wide statistical rejection) and remains subject to the open Research Verification Gate (Gate B). (`.omc/research/spain-belgium-live-probe.md`.)
- **The first dust run is an execution-safety and learning test, not an edge proof.** If it runs, it proves *one* bounded, reconciled real order lifecycle and that the safety gates fire. It proves nothing about profitability or generalization, and it is labelled accordingly (`DUST_LIVE` / `UNCALIBRATED` / `NOT_PROVEN_EDGE`).
- **No maker edge is claimed.** A *precise* maker-fill backtest isn't available — Polymarket serves no historical book, and third-party L2 archives give depth but not exact per-order queue position, so any maker-fill number is *modeled*, not measured. FV-lead does not prove maker profitability.
- **There is no realized-PnL claim, no fill claim, and no "profitable bot."** The candidate CLV signal is directional evidence over ~18 fixtures; its venue translation was falsified.

The honest state of this project is: a rigorously provable arena, a candidate signal that has not been shown to survive contact with a real venue, and an open empirical question I built the right instrument to answer. For a non-expert who let the evidence drive every call, I think that is the credible place to be — and every claim in it is one a judge can re-run and check.

---

*Provenance for the claims above lives in the repo's sealed runs and research log — the lead-lag probe (`524d27b`), the live-monitor findings, the R3 recorder contracts, the run notes, and the independent-panel synthesis — so none of this rests on my summary alone.*
