# Predeclared evaluation protocols (S6 / CON-008)

This directory holds the **committed** evaluation protocols the S3/S6 multi-fixture evaluation
runs against. Committing the protocol here is a **trust gate**, not paperwork.

## The rule (CON-008)

> A protocol file MUST be committed **before** the first real S3/S6 run. The run is **one pass**,
> and it **reports whatever that pass yields** — winners and losers alike.

Concretely:

- `veridex.backtest.evaluation.run_multi_fixture_evaluation` reads a **committed** `EvalProtocol`.
  It **never synthesizes a protocol at runtime**, never re-orders the roster to flatter a result,
  and never hides a losing number after the fact.
- Because the fixtures, the strategy roster, the window/close semantics, and the baseline floor are
  all fixed *before* anyone sees a result, the evaluation cannot be quietly reshaped into a
  cherry-picked subset once the numbers land. Pre-commitment is what makes "report whatever the pass
  yields" honest rather than a slogan.

## What a protocol pins

An `EvalProtocol` (see `veridex/backtest/evaluation.py`) declares:

| field | meaning |
|-------|---------|
| `protocol_id` | stable id for this committed evaluation |
| `fixture_ids` | the fixtures the roster is evaluated over |
| `strategy_configs` | the roster of strategy-config ids (e.g. `cumulative-drift`, `value-vs-venue`, `stale-line`) |
| `window` | the window id every fixture run is scored under |
| `close_semantics` | the window `end_rule` (`pre_match` yields true CLV) |
| `baselines` | the named zero-edge baselines the roster is compared against (never alpha) |
| `committed_at` | ISO-8601 commit stamp — the pre-run commitment |

## Honesty gates enforced by the runner

- **StaleLine is cadence-gated (AC-009).** A predeclared `stale-line` strategy is admitted into the
  report **only** when the recorded-quote cadence actually backs sub-minute freshness
  (`cadence_ok`, sourced from `veridex.venues.quote_recorder.cadence_report`). Otherwise it is
  dropped — it is never run, and never reported, on cadence it cannot justify.
- **Every metric carries an evidence rung.** Each surfaced metric is tagged with one of the five
  `veridex.provenance.EvidenceRung` labels. CLV-family metrics are TxLINE-sealed (`txline-only`);
  the venue-derived estimated executable edge (present only when the roster runs `value-vs-venue`)
  is `backfilled-price-history`.
- **Nulls and abstentions are counted honestly.** Rows with no closing CLV (`clv_bps is None`) and
  WAIT abstentions are counted and reported — never silently dropped, never scored as `0`.

## Adding a protocol

1. Author the `EvalProtocol` and serialize it into this directory (one committed file per protocol),
   **before** the run.
2. Commit it. The commit is the pre-registration.
3. Run the evaluation against the committed file. Publish the full result — including any baseline
   that beat the roster, and any fixture that lost.
