# MM-R3 / MM-R4 recorder checklist

This is the checklist a **future, operator-gated live-data recorder** must satisfy
*before* any MM-R3 or MM-R4 claim is admissible. Until then both rungs are
**DECLARED but OUT OF SCOPE**, and the code enforces that: the MM-2
data-feasibility gate (`veridex/maker/rung_gate.py`) caps at **MM-R1.5**, and no
R3/R4 execution symbol may be defined anywhere in `veridex/maker/*` (guarded by
`tests/test_no_r3_r4_code.py`).

No R3/R4 result exists today. This document declares requirements only — it
invents no numbers and asserts no measured fill, edge, or PnL.

## The 4-rung ladder (where R3/R4 sit)

| Rung | What it measures | Status |
| --- | --- | --- |
| **MM-R1** | Forward-markout quote-quality vs future TxLINE FV | BUILT, scored |
| **MM-R1.5** | Trade-aware adverse-selection diagnostic | BUILT, report-only |
| **MM-R2** | Fill-assumption sensitivity bracket | BUILT, `UNCALIBRATED`, never ranked |
| **MM-R3** | Queue-position fill **simulation** | DECLARED, out of scope |
| **MM-R4** | Real **own-fill** reconciliation | DECLARED, out of scope |

## Why R3/R4 are out of scope NOW

The venue data is **mids-only**: there is no L2 order book / depth, no cancel
stream, and no own-order lifecycle. Any "fill" produced from this data would be an
**assumption**, not a measurement — which is exactly what MM-R2's `UNCALIBRATED`
bracket already surfaces (`veridex/maker/r2_bracket.py`: `queue_modeled` is forced
`False`; a `True` is rejected at construction because "depth is unavailable on
mids-only data, so queue position can never be modeled"). Turning an assumption
into a ranked "fill" would fabricate the very number the platform refuses to fake.
`real_executable_edge_bps` therefore stays the pinned literal `None`
(`veridex/maker/result.py`, `veridex/maker/scorer.py`, `veridex/maker/diagnostic.py`).

## MM-R3 (queue-position fill simulation) requires a recorder capturing

- **L2 `BookSnapshot`** — full depth at each price level at a reference instant.
- **Incremental `BookDelta`** — per-level depth changes between snapshots, so book
  state can be reconstructed tick-by-tick.
- **The trade / print stream** — partly available today via the MM-R1.5
  `TradePrint` feed (Polymarket `OrderFilled` prints between *other* participants),
  but that alone is insufficient: prints are not depth.
- **Enough book state to model queue position** ahead of our resting order at each
  level (arrivals, cancels, and fills ahead of us).

Explicit boundary: **without depth + queue state, a simulated fill is fiction.** A
queue-position simulator built on mids cannot know whether our order would have been
filled, so it must not exist in the scored lane.

## MM-R4 (real own-fill reconciliation) requires

- **Our OWN order lifecycle** — `place` / `amend` / `cancel` / `partial-fill` /
  `fill` events tied to **our own order ids**, i.e. we actually quoted on-venue.
- **Own `OrderFilled` matched to our submissions** — reconciled against the venue's
  settlement, not inferred from anonymous market prints (today's `TradePrint`
  deliberately omits any `fill_price` / `pnl` / `real_executable_edge_bps` field
  precisely because it is *never our own fill*; see `veridex/maker/trades.py`).

Only once own fills are observed and reconciled can `real_executable_edge_bps`
become a **measured** value instead of the pinned `None`.

## The gate

The MM-2 data-feasibility gate activates a rung **only when the columns it needs are
physically present**. `DataPresence` accepts `has_l2_depth`, `has_cancels`, and
`has_own_fills`, but `assign_rung` deliberately **ignores** them: with every flag
set `True` it still returns **MM-R1.5**, never R3/R4. Until the recorder above
exists **and is independently verified**, `assign_rung` caps at MM-R1.5 and
`real_executable_edge_bps` stays `None`.

This document is the checklist that a future operator-gated recorder step must
satisfy before any MM-R3 or MM-R4 claim can be made.
