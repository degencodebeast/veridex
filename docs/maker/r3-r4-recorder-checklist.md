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

## R1.5 → R3/R4 handoff: what the `OrderFilled` tape already provides, and what stays future-only

R1.5's normalized `TradeArtifact` (built from the Polymarket `OrderFilled` prints
between *other* participants — see `veridex/maker/trade_artifact.py` and
`veridex/maker/capture.py`) is a real, sealed, on-chain trade record. It is a
genuine step toward R3/R4, but only for the **trade portion** of what each rung
needs — it does not, and cannot, close the rest of the gap.

**What it PROVIDES toward R3 (trade/print side only):**
- Real venue prints with chain-event identity (`block_number`, `tx_hash`,
  `log_index`) and native-probability `price`/`size`/`aggressor_side` —
  i.e. exactly the "trade / print stream" bullet in the MM-R3 checklist above.
- A predeclared, hash-pinned provenance chain (`TradeArtifact.artifact_hash`
  bound to `MakerRunConfig.trade_artifact_hash`, verified before any I/O) —
  so *when* a future depth recorder exists, the trade side of the R3 join
  already has an auditable, non-fabricated source to join against.

**What remains FUTURE-ONLY for R3 (still missing, unaffected by R1.5/R2):**
- **Historical L2 `BookSnapshot` / incremental `BookDelta`** — full depth at
  each price level, reconstructable tick-by-tick. R1.5/R2 touch none of this;
  the venue is still mids-only. Without it, queue position ahead of a resting
  order cannot be modeled — a simulated fill would remain fiction.
- **Cancel stream** — arrivals/cancels ahead of us in the queue. Not captured
  by `OrderFilled` prints (which only ever record *executed* trades), and not
  added by R1.5/R2.
- Both are **irrecoverable** for any block range that was not recorded live
  with depth+cancels at the time — they cannot be backfilled from prints alone.

**What remains FUTURE-ONLY for R4 (untouched by R1.5/R2):**
- **Our own order lifecycle** (`place`/`amend`/`cancel`/`partial-fill`/`fill`
  tied to our own order ids) — R1.5's `TradePrint`/`NormalizedTradeRow` are
  deliberately anonymous market prints between *other* participants; they
  carry no `fill_price`/`pnl`/`real_executable_edge_bps` field and cannot be
  attributed to "our" order by construction (`veridex/maker/trades.py`).
- **Real own-fill reconciliation** — matching our own submissions against the
  venue's settlement. This requires us to have actually quoted on-venue,
  which R1.5/R2 (report-only diagnostics and ex-ante sensitivity brackets over
  *others'* prints) do not do and are not intended to do.
- Until both exist and are independently verified, `real_executable_edge_bps`
  stays the pinned literal `None`, and `assign_rung` continues to cap at
  MM-R1.5 regardless of any `has_l2_depth`/`has_cancels`/`has_own_fills`
  presence flag (see `tests/test_no_r3_r4_code.py`).

In short: R1.5/R2 close the **trade-identity** gap for R3 and add nothing toward
R4 (by design — R4 requires *our own* fills, which this extension never
produces). Depth+cancels for R3 and own-order lifecycle for R4 remain the two
irreducible, recorder-gated prerequisites named above.
