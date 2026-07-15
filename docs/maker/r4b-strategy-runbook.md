# R4-B Strategy Operator Runbook — venue-anchored, TxLINE-guarded maker (offline)

**Status:** `EXPERIMENTAL_DUST` · `UNCALIBRATED` · `NOT_PROVEN_EDGE` · **proposal-only** ·
offline / replay / dry-run **only** · **no live path in this lane**.

This runbook is the guide to the R4-B maker **strategy policy** lane
(`veridex/mm_strategy/`): a pure, deterministic policy that *proposes* neutral market-maker
intents from a venue book, a market-status feed, and (when its guard is armed) a TxLINE
fair-value (FV) signal. It documents the honesty labels, the offline A/B ablation harness and
how to read its six-metric report, the run-receipt provenance, the R4-A authority boundary,
and — most importantly — the **claim ceiling** every reader must hold.

> **Read this first — the claim ceiling (the whole point of this lane).**
> R4-B has **not** proven an edge. It has **not** made, and cannot make, a profit claim. It is
> an **experimental, uncalibrated, proposal-only policy**. The only thing the A/B ablation
> establishes is **functional divergence**: with the TxLINE guard armed, the policy produces a
> *different decision stream* on matched opportunities than with the guard off. Divergence is a
> statement about *machinery*, not about *money*. Nothing in this lane is a signal to trade
> live, to fund a wallet, or to arm a live order. Promotion, edge claims, and any judge-facing
> "it works" statement are **Gate B** concerns, out of scope here (see [§8](#8-the-operator-only-boundary-outside-this-lane)).

---

## 1. What R4-B is — and what it is emphatically not

**Is:** a pure, deterministic **policy proposal** engine. Given a minted observation stream
(venue book + market status + optional guard FV), the one pure `decide()` core
(`veridex/mm_strategy/core.py`) emits a `StrategyDecision` — a *neutral* market-maker intent
plan (place a two-sided quote, pull one side, cancel, or abstain). It anchors to the **venue
mid** (never to FV), applies a transition table, and — only when the guard is armed — lets a
freshness-gated TxLINE FV *nudge which side it rests*.

**Is not:**

- **Not an edge.** No metric in this lane is evidence of profit, positive expected value, fill
  capture, or capacity. See the claim ceiling above and [§4](#4-reading-the-six-metric-report).
- **Not an executor.** R4-B never sizes, signs, submits, cancels on the wire, or touches a
  wallet. It *proposes* neutral intents; **R4-A** (`veridex/dust_execution/`) remains the sole
  execution / sizing / signing / wire authority (see [§6](#6-the-r4-a-authority-boundary)).
- **Not calibrated.** `UNCALIBRATED` — no calibration has been established. The configuration
  is a set of hash-pinned priors, not tuned parameters.
- **Not live-armable here.** This lane is **offline / replay / dry-run only**. There is **no
  live smoke test** in R4-B and **no promotion path** within R4-B. A live arming, a first real
  order, funding, or a Mode-B arm is **operator-only** and outside this plan
  ([§8](#8-the-operator-only-boundary-outside-this-lane)).

## 2. The mandatory honesty labels

Every R4-B run-receipt (`RunReceipt`, see [§5](#5-the-run-receipt--provenance-and-fail-closed-labels))
carries three **pinned** labels:

| Label            | Pinned value        | Meaning |
|------------------|---------------------|---------|
| `evidence_class` | `EXPERIMENTAL_DUST` | A defined-but-unvalidated policy. Admits with **no** profitability flag. |
| calibration      | `UNCALIBRATED`      | No calibration has been established. |
| edge             | `NOT_PROVEN_EDGE`   | **No edge is claimed or implied.** |

These are structurally enforced, not merely written down. The evidence class is **hardcoded**
`EXPERIMENTAL_DUST` while Gate B is `OPEN` / `STALE`; a caller-supplied
`requested_evidence_class` has **zero effect** — a request to relabel a receipt `PROMOTED`
**fails closed** and the receipt stays `EXPERIMENTAL_DUST`. There is deliberately **no**
`expected_pnl` / `edge_bps` / `benefit` field anywhere on the receipt, the report, or the
conclusion, so the artifacts structurally cannot narrate a profit or an edge.

Pinned identity carried on every receipt:

- `strategy_id = "venue-anchored-txline-guarded-maker"`
- `revision   = "r4b-v0"`

## 3. Running the offline A/B ablation

The ablation lives in the **test-side** harness `tests/mm_strategy_ablation_harness.py`
(a helper module — it is never imported by production or by any ranked lane). It is exercised
by `tests/test_mm_strategy_ablation.py` and by the whole-lane capstone
`tests/test_mm_strategy_integration.py`. Everything runs **offline** against frozen fixture
tapes; there is no network, no wallet, and no live call.

Run the ablation and whole-lane suites (from `ARENA_ROOT`, the `veridex-arena` repo root):

```bash
.venv/bin/python -m pytest -q tests/test_mm_strategy_ablation.py tests/test_mm_strategy_integration.py
```

To drive an A/B replay yourself in an offline session (illustrative — this is a diagnostic
harness, not a live entry point), the two arms are identical **except for the guard**:

```python
from tests.mm_strategy_ablation_harness import (
    load_tape, arm_configs, load_base_config_overrides,
    replay_arm, matched_opportunity_report, arm_single_metrics, ablation_conclusion,
)

tape  = load_tape("healthy")                       # a frozen offline fixture tape
arms  = arm_configs(load_base_config_overrides())  # baseline = guard OFF, guarded = guard ON
                                                   # the two configs differ ONLY by guard_enabled

baseline = replay_arm(tape, arms.baseline, session_dir=...)   # guard-off arm
guarded  = replay_arm(tape, arms.guarded,  session_dir=...)   # guard-on arm

report = matched_opportunity_report(baseline, guarded)        # six metrics, venue-referenced
concl  = ablation_conclusion(report,
                             arm_single_metrics(baseline),
                             arm_single_metrics(guarded))
```

**What the A/B proves — and only this.** The baseline arm is FV-blind by construction: with
the guard off the policy emits `guard_fv=None` and never reads the FV cache, so a guard-off
replay is **byte-identical across FV health** (absent / healthy / stale / reconnecting). The
guard-on arm is the *same core* with the *same config except* `guard_enabled=True`. So any
difference between the two arms is attributable to **the guard alone** — this is **functional
divergence**: the guard changes the decision stream on matched opportunities. It is **not**
evidence that the guarded stream is better, more profitable, or ready to trade. A guard-on arm
that diverges is a working switch, nothing more.

## 4. Reading the six-metric report

`matched_opportunity_report(baseline, guarded)` returns the **six mandatory metrics together**
— there is no partial-report path, so no flattering subset can be presented in isolation:

| Metric | What it is | What it is **not** |
|---|---|---|
| `per_fill_markout` | Mean venue-referenced markout over the guarded arm's candidate fills. | Not PnL. Not realized. |
| `matched_opportunity_markout` | Paired guarded-minus-baseline markout over the **same** eligible opportunities (keyed by `(observation_index, leg_role)`, intersection only). Strips the "guarded traded less" selection bias. | Not an edge. The one input to the hypothesis, still pending Gate B. |
| `exposure_normalized_adverse_selection` | Adverse-markout mass normalized by capital at risk. | Not a loss/profit figure. |
| `fill_count` | Candidate-fill count on the guarded arm. | "Fewer trades" is **not** "better". |
| `abstention_count` | How often the guarded arm abstained. | Not a quality signal on its own. |
| `capital_at_risk` | Notional the guarded arm rested. | Not committed capital — offline. |

**Markout reference is venue-derived — never FV.** Every markout is scored against the venue's
**own** future mid at the *next venue change* (event-time), which is the honest ceiling model
(no queue model, no own-fill claim). Scoring the FV-driven guard against the *same* FV it
consumes would be circular self-validation, so the harness **fails closed** with
`MarkoutReferenceError` if asked to score against `"fv"` (`reference="venue"` is pinned).

**The honest conclusion shape.** `AblationConclusion` has **no** `benefit` / `better` /
`winner` field, and `infers_benefit()` **always returns `False`**. The three
forbidden-alone comparisons — total markout, fill count, per-fill markout — are *recorded* on
the conclusion for transparency, but the verdict is structurally blind to them: a lower total
loss, fewer trades, or a nicer per-fill markout can **never** become a benefit claim. The only
statement the harness will ever yield is the pinned shape:

> **"risk-edge hypothesis on matched opportunities, pending Gate B"**

Read it exactly that way: a *hypothesis*, over *matched opportunities*, **pending Gate B**. It
is not a result, not a recommendation, and not a profit claim.

**A losing session is operational success, not failure.** A bounded-dust replay that ends with
a *negative* markout is an **operational success** — it proves the machinery is correct
(deterministic, produced intents, abstained honestly, stayed within bounds) — **not** a
failure and **not** evidence of edge. The `_dust_operational_success` verdict is
`deterministic ∧ produced_intents ∧ honestly_abstained ∧ within_bounds`; it **never consults
markout**, and its verdict is identical across losing / flat / winning arms. PnL and
correctness are orthogonal here by design.

## 5. The run-receipt — provenance and fail-closed labels

Each replay can be pinned into a frozen `RunReceipt` that records the exact provenance of the
run so it is reproducible and un-launderable:

- `strategy_id`, `revision` (`r4b-v0`), and `config_hash` — the exact policy identity.
- the observation hashes, the prior→next `state_hash` chain (linkage asserted), the
  `decision_id`s, and a `decisions_digest` — the exact decision lineage.
- the Gate-B status and the Gate-B evidence revision it was observed against.

**Reproduction:** replaying the original config re-mints a **byte-identical** receipt; revising
the config produces a *new* `config_hash` (a different run), while the original config still
replays byte-identically. **Relabel fails closed:** `requested_evidence_class` has no effect;
the emitted `evidence_class` stays `EXPERIMENTAL_DUST` while Gate B is `OPEN` / `STALE`. There
is no code path by which a receipt narrates itself as `PROMOTED`, calibrated, or edge-proven.

## 6. The R4-A authority boundary

R4-B **only proposes**. R4-A (`veridex/dust_execution/`) remains the **sole** execution,
sizing, signing, and wire authority. The strategy emits a *neutral* intent, which the
execution adapter (`veridex/mm_strategy/execution_adapter.py`) maps one-to-one onto an R4-A
request:

| R4-B neutral intent  | R4-A intent   |
|----------------------|---------------|
| `place_quote`        | `make_quote`  |
| `replace_quote`      | `cancel_replace` |
| `cancel_all_orders`  | `cancel_all`  |
| `abstain`            | `no_quote`    |

The image is **exactly** those four; there is **no `take`** in the image (taker action is
unreachable by design), the mapping carries **no strategy-owned size** and no raw wire handle,
and `evidence_class` is a **pinned** `EXPERIMENTAL_DUST` constant, not a caller parameter. The
R4-A request derives its token from the reviewed observation; **R4-A owns the size**. Plans are
**single-phase** — cancel XOR place, never both. A `NO_QUOTE` with existing exposure funnels to
`cancel_all_orders` → `abstain` (cancel exposure first); a `NO_QUOTE` with no exposure emits
**zero** intents (never a bare cancel). Nothing R4-B produces reaches the wire on its own.

## 7. Offline / containment posture

The lane is offline and contained by construction, and the whole-lane capstone test proves it:

- **No network.** The capstone
  `tests/test_mm_strategy_integration.py::test_whole_lane_no_direct_wire_no_live_call` **bans
  `socket.socket` for the entire run** — the facade sees only typed neutral intents, never a
  wire primitive; Mode-A dry-run only.
- **No secrets, no wallet, no signer.** No credentials are present; no signing surface is
  imported by the strategy, adapter, assembler, or runtime.
- **Sealed artifacts untouched.** The three sealed JSONs
  (`contracts/fixtures/leaderboard.json`, `contracts/fixtures/maker_arena_result.json`,
  `scripts/txline_live/cp1/maker-arena-result.json`) are **byte-identical** to HEAD after any
  replay.
- **Not rankable, not reverse-importable.** R4-B diagnostic fields are rejected by all three
  rank surfaces (`veridex.scoring`, `veridex.leaderboard`, `veridex.maker.leaderboard`), and no
  ranked lane imports `veridex.mm_strategy`. No `veridex.research.*` import exists anywhere in
  the strategy.

## 8. The operator-only boundary (outside this lane)

Everything below is **operator-only** and **outside this plan**. R4-B does **not** provide,
document, or enable any of it, and this runbook is not a path to any of it:

- funding or selecting a wallet; capital-at-risk selection;
- Privy / TWAK / local signer setup; venue credentials;
- **live arming; the first real order authorization; a Mode-B arm;**
- Agent Studio deployment; AgentOS production hosting;
- **Gate-B approval / promotion / closure;**
- **any claim that the strategy has an edge or is profitable.**

This lane never ends with a live smoke test, and there is no promotion within R4-B. The
evidence produced here stays **unpromoted**, pending Gate B.

## 9. One-line honesty checklist for any R4-B claim

Before writing or repeating any statement about R4-B, confirm all of the following:

- [ ] It says **proposal-only**, `EXPERIMENTAL_DUST` / `UNCALIBRATED` / `NOT_PROVEN_EDGE`.
- [ ] It claims at most **functional divergence** on matched opportunities — never edge,
      profit, PnL, fill capture, or capacity.
- [ ] It frames any result as a **hypothesis pending Gate B**, never a conclusion.
- [ ] It describes the lane as **offline / replay / dry-run only** — no available live path.
- [ ] It attributes execution / sizing / signing to **R4-A**, and promotion to **Gate B**.

If a sentence could be read as "this makes money" or "this is ready to trade live," it is
wrong for this lane — rewrite it.
