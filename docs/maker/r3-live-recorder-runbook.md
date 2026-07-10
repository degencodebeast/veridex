# MM-R3 live-recorder — operator runbook + R4 handoff

**Ship status (honest): MM-R3 recorder complete + R4 declared/gated, not run.**

This is the operator runbook for the MM-R3 **live-recorder** lane
(`veridex/live_recorder/*`, driven by `scripts/maker/live_recorder.py`). It records a
sealed, replayable, append-only stream of market evidence and **sends no orders**. It also
declares the MM-R4 real-money handoff — its event contracts, its four go-live gates, and the
two safety gaps that must be closed first — **as a declaration only**. No R4 code exists in
this lane, and this document invents no fill, no edge, and no PnL.

To be exact about scope: **R3 recorder complete; R4 declared/gated, NOT run.**

---

## 1. What the R3 lane records

The recorder streams TxLINE fair value, polls the **public** Polymarket `/book` for full
depth, aligns the two streams with a strict no-look-ahead rule, and appends one JSON line
per event to a sealed session. Every event type is an immutable, `extra="forbid"` pydantic
contract in `veridex/live_recorder/contracts.py`:

| Evidence | Event contract | What it is (and is not) |
| --- | --- | --- |
| **Fair value** | `FairValueEvent` | A TxLINE FV observation with an HONEST proof reference. When the source carries no `message_id`, `proof_status` is pinned to `unavailable_no_message_id` — a proof is never fabricated. |
| **Book depth** | `VenueBookSnapshotEvent`, `VenueBookDeltaEvent` | Full `(price, size)` depth levels, never a collapsed mid. An empty book side is a legitimate empty tuple, never imputed. |
| **Venue trades** | `VenueTradeEvent` | Observed venue prints (optionally with on-chain `block_number`/`tx_hash`/`log_index`) between *other* participants — never our own fill. |
| **Decision intent** | `DecisionEvent` + `QuoteIntentEvent` / `TakeIntentEvent` / `NoQuoteIntentEvent` | The decision-time context (reference hashes, `recv_ts`) and the intent. Decision-time fields only — no post-decision outcome is ever stored on the intent. |
| **Counterfactual executability** | `ExecutabilityMeasurement` (carried on `TakeIntentEvent`) | What it *would* cost to clear against the observed book — `label` is pinned to the literal `"COUNTERFACTUAL"`. There is deliberately no `fill_price` / `filled_size` / `realized_pnl` / `real_executable_edge_bps` field. |
| **Latency** | `LatencyEvent` | Per-stage chain latency (fv→book, book→decision) in integer ms. |
| **Risk gates** | `RiskGateEvent` | Each decision-time gate evaluation (`pass` / `block`). |
| **Gaps** | `RecorderGapEvent` | An explicit, labeled gap in the source stream — never a silent splice. Gaps are covered by the session content hash and excluded from analysis. |
| **Replay checkpoints** | `ReplayCheckpointEvent`, `RecorderHeartbeatEvent` | Rolling partial content-hash checkpoints and liveness beats so a crash-partial session is still verifiable. |

The default operator policy in the CLI **abstains** (`no_quote`) on every poll, so a session
captured with the stock command records genuine market evidence (FV, depth, latency, gaps)
plus honestly-labeled no-quote decision context — and never fabricates a trading intent. A
strategy `decide_fn` is a pluggable seam on
`veridex.live_recorder.runner.run_live_recorder`; when an operator supplies a take policy,
the runner fills the counterfactual `ExecutabilityMeasurement` itself (via
`veridex.live_recorder.executability.measure_take`) so the policy can never invent it.

## 2. Trust properties (stated plainly)

- **No orders.** The CLI and runner reference no order-submit / order-cancel / order-place /
  venue-write symbol and construct no order-placing, funded venue client. They record
  evidence only.
- **No fills, no realized PnL.** Nothing here is a fill. Executability is **counterfactual
  only** — "what it would cost to clear against the observed book at time T", never "what we
  got". There is no realized PnL and no rankable value produced by this lane.
- **Two-dimensional no-look-ahead.** Each incoming FV is recorded with its arrival `recv_ts`
  (integer ms); a decision aligns FV using the decision's *own* `recv_ts`, so a decision can
  only ever see FV that had arrived by that instant (`veridex/live_recorder/alignment.py`).
- **Sealed, tamper-evident replay.** The session is sealed with a canonical content hash over
  the full sequence-ordered event stream (including gap markers). Replaying the session
  reproduces a **byte-identical** result; any edited line breaks the hash
  (`veridex.live_recorder.replay.replay_reproduces`).
- **Fail-closed secrets, no token logging.** Both TxLINE credentials are required before any
  I/O; a missing credential exits immediately. No secret value is ever logged, and artifacts
  carry only a boolean `txline_configured` flag — never the secret.

## 3. How to run the CLI

The command mirrors `scripts/maker/live_monitor.py` argument style. Set both TxLINE
credentials in the environment (`JWT` and `TXLINE_X_API_TOKEN`), then:

```bash
.venv/bin/python -m scripts.maker.live_recorder \
  --fixtures scripts/txline_live/cp1/fixtures.json \
  --out .omc/research/live-recorder \
  --poll-interval-ms 5000 \
  --minutes 30
```

| Flag | Meaning | Default |
| --- | --- | --- |
| `--fixtures` | Path to `fixtures.json` (`fixture_id` / `event_slug` / `home_team` / `away_team`). | required |
| `--out` | Session output root; the session is written under `--out/<session_ts>/`. | `.omc/research/live-recorder` |
| `--poll-interval-ms` | Milliseconds between venue-book poll rounds. | `5000` |
| `--minutes` | Session wall-clock budget. | `30` |
| `--base-url` | Override the TxLINE base URL (e.g. `https://txline.txodds.com/api` for mainnet). | config default |

If either credential is absent the command **fails closed before any I/O** — no session
directory is created and no secret is echoed. Stop the session at any time with `Ctrl-C`
(SIGINT); shutdown seals `meta.json` cleanly.

## 4. Session output layout

```
<--out>/<session_ts>/
  records.jsonl   # append-only, one JSON event per line, monotonic sequence_no
  meta.json       # sealed session provenance: session_ts, endpoints, tool_version,
                  #   config_hash, source_provenance (incl. boolean txline_configured),
                  #   fixture_ids, event_count, ended_ts, content_hash
```

`content_hash` in `meta.json` is the sealed commitment over the whole event stream. Keep the
directory intact — the sealed hash is what makes the session replayable and tamper-evident.

## 5. How to replay + analyze

Read a sealed session back and produce its gap-excluded, observation-only analysis with
`veridex/live_recorder/analysis.py`:

```python
from veridex.live_recorder.analysis import analyze_session, render_session_report
from veridex.live_recorder.replay import replay_reproduces

session = ".omc/research/live-recorder/<session_ts>"

# 1. Verify the sealed session replays byte-identically (tamper-evident).
assert replay_reproduces(session)

# 2. Observation-only analysis: cadence, lead-lag, queue-jump — all gap-excluded.
result = analyze_session(session)
print(render_session_report(result))
```

`render_session_report` emits an observation-only Markdown report. Every executability
reference is labeled `COUNTERFACTUAL`; the only claims it makes are observed-size-at-price-
at-T, gap-excluded FV cadence, no-look-ahead replay-reproduced evidence, and R4-prerequisite
met / not-met status. When the lead-lag probe's own verdict does not confirm a lead, the
report says so directly — it never embellishes a lead that the evidence does not support.

## 6. Ship line

> **MM-R3 recorder complete + R4 declared/gated, not run.** The recorder captures sealed,
> replayable, no-look-ahead evidence with counterfactual-only executability. It produces no
> fills, no realized PnL, and no rankable value. MM-R4 (real-money) is declared in the R4
> handoff section (appended next) and gated — it is not implemented in this lane.
