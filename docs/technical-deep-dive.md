# Veridex — Technical Deep-Dive

> **Agents can trade. They can't grade themselves.**
> Veridex is the proof-and-deployment layer for autonomous sports-trading agents. This document
> is the complete technical account of how it works: every design decision, every trust boundary,
> every honest limitation, with file-and-line references into the codebase so any claim can be
> checked against the source that enforces it.

**Audience.** Two readers, one document: (1) the founder preparing to defend every design decision
in a live judge interview; (2) the future reader of the platform's public technical docs. Nothing
in the system should be mysterious after reading this.

**Grounding discipline.** Every guarantee stated here names the code that implements it and, where
applicable, the test that enforces it. Where something is designed but not wired (custody/payouts),
or built but never exercised with real money (the live order path), this document says so plainly —
that honesty is itself a load-bearing feature of the product.

**Verification status at time of writing.** The full backend suite (1,007 collected tests) passes
offline: `.venv/bin/python -m pytest tests -q`.

---

## Table of contents

1. [Executive overview](#1-executive-overview)
2. [The data layer — TxLINE ingestion end-to-end](#2-the-data-layer--txline-ingestion-end-to-end)
3. [The runtime — one run, N agents, one seal](#3-the-runtime--one-run-n-agents-one-seal)
4. [The law and scoring](#4-the-law-and-scoring)
5. [The proof layer](#5-the-proof-layer)
6. [Policy and execution safety](#6-policy-and-execution-safety)
7. [Price-unit and display honesty](#7-price-unit-and-display-honesty)
8. [Venues — the adapter seam and Polymarket](#8-venues--the-adapter-seam-and-polymarket)
9. [The live-money conjunction](#9-the-live-money-conjunction)
10. [Strategies — Momentum v1 and Sharp Momentum v2](#10-strategies--momentum-v1-and-sharp-momentum-v2)
11. [The deploy platform](#11-the-deploy-platform)
12. [Backtesting and the real-data experiment](#12-backtesting-and-the-real-data-experiment)
13. [The frontend](#13-the-frontend)
14. [The trust-boundary registry](#14-the-trust-boundary-registry)
15. [Testing philosophy](#15-testing-philosophy)
16. [Design-decision ledger](#16-design-decision-ledger)
17. [Hard-questions appendix (interview prep)](#17-hard-questions-appendix-interview-prep)
18. [Glossary](#18-glossary)

---

## 1. Executive overview

### 1.1 The problem

Every "my AI bot made 40%" claim is unverifiable. The model could be cherry-picking its window,
peeking at the future, re-de-vigging its own odds, or simply lying — and the agent that *reports*
its performance is the same agent being *graded*. That conflict of interest sits at the center of
every autonomous-trading demo, and no amount of dashboard polish fixes it. Trust-me numbers are
worth nothing in trading.

Veridex's answer is structural, not procedural: **separate the agent from its own grading, by
construction.** The agent may only *propose* a constrained action. Everything that becomes a
score, a rank, or a proof is recomputed by deterministic code from sealed evidence that the agent
cannot touch.

### 1.2 The six-link chain

The whole product is one chain, and no link trusts the previous one:

```
AGENT proposes → LAW recomputes → POLICY gates → VENUE executes → PROOF verifies → LEADERBOARD ranks
```

| Link | Code | What it may do | What it may NOT do |
|---|---|---|---|
| **Agent** | `veridex/strategies/`, `veridex/runtime/agent.py` | Emit a constrained `AgentAction` (`veridex/runtime/schemas.py:25`) with `market_key`, `side`, and *untrusted* rationale metadata | Score itself. Its `claimed_edge_bps` is recorded, then ignored (`veridex/law/recompute.py:156-157`) |
| **Law** | `veridex/law/recompute.py` | Deterministically recompute edge/CLV/validity from sealed market snapshots | Read any agent-claimed number as an input |
| **Policy** | `veridex/policy/gate.py` | Approve/deny/escalate execution under operator limits, in two phases around the venue quote | Alter a score; the gate is safety, never skill |
| **Venue** | `veridex/venues/` | Quote and (behind many locks) execute | Feed a receipt back into scoring — receipts are structurally non-scoring (`evidence=False`, §5.5) |
| **Proof** | `veridex/checks/`, `veridex/verifier/` | Recompute everything fresh from sealed bytes and render a falsifiable verdict | Hardcode a PASS. Every check can fail (§5.3) |
| **Leaderboard** | `veridex/scoring.py`, `veridex/leaderboard.py` | Rank on recomputed CLV only | Rank on confidence, Kelly, proof-completeness, or anything an agent claims (`veridex/leaderboard.py:203-229`) |

### 1.3 The four pillars

1. **Agent Studio** — configure and deploy agents from strategy templates. Typed, bounded configs;
   a fail-closed named preflight; one flow `configure → preflight → deploy → observe → verify`
   (`veridex/api/deploy.py`, `veridex/deploy/`).
2. **Live Agent Arena** — N agents compete concurrently *on identical sealed inputs*: same ticks,
   same closing snapshot per market, same law, same policy (`veridex/runtime/orchestrator.py`).
   Rank differences are strategy, never luck of the feed.
3. **Verification / proof layer** — sealed evidence hashes, seven structural checks
   that recompute rather than assert, a Merkle root-forest, a Solana Memo anchor, and
   `POST /runs/{id}/verify` (`veridex/api/router.py:617`).
4. **Execution + risk layer** — a two-phase policy gate, a pure circuit breaker, stake caps,
   quote-size coupling, five honestly-labeled run modes, and a fail-closed, operator-only path to
   real money (`veridex/policy/`, `veridex/execution/`, `veridex/competition/service.py`).

### 1.4 The AgentInstance formula

```
AgentTemplate + AgentConfig + PolicyEnvelope + Evidence = AgentInstance
```

The **template** is the strategy family (`baseline`, `momentum`, `momentum-sharp`, `llm` —
`veridex_agent/config.py:60`). The **config** is the actual strategy — every behavioral knob,
typed and bounded, folded into a `config_hash` (`veridex/deploy/preflight.py:115-125`). The
**policy envelope** is the execution boundary, with its own `policy_hash`
(`veridex/policy/envelope.py:58-65`). A deploy pins all of it into a **durable, store-backed
`AgentInstance`** (`veridex/deploy/instance.py:78-125`). Two users can deploy the same template
with different configs, rank head-to-head on the same falsifiable leaderboard, get different
performance — and Veridex proves exactly what each one did.

The corollary is the platform's central rule: **configs change behavior; no config can change the
trust rules.** Nothing a user submits can bypass the law, the evidence hash, the checks, receipt
separation, or scoring immutability.

### 1.5 The trust thesis

Most agent platforms ask you to trust their leaderboard. Veridex ships a leaderboard you can
*falsify*: the verify endpoint re-runs the deterministic law over the sealed event log, and
tampering with one sealed byte turns the proof red. The system is honest enough to prove when an
agent had **no** edge — and it did exactly that on real World Cup data (§12). A platform that
manufactures edge is worthless; a platform that proves exactly what an agent did — including
honest abstention and unscoreable closes — is the missing trust layer for autonomous trading.

---

## 2. The data layer — TxLINE ingestion end-to-end

### 2.1 Authentication: guest JWT → on-chain subscribe → API token

Live-feed access follows a three-step flow implemented in `veridex/ingest/txline_auth.py`:

1. `POST /auth/guest/start` mints an IP-bound guest JWT (`txline_auth.py:26-42`).
2. An **on-chain `subscribe()` transaction** is sent on Solana devnet — a real signed transaction
   (free for the World Cup tier), built from a typed-config keypair
   (`txline_auth.py:45-96`). The devnet signature is publicly checkable (linked in `README.md:107`).
3. `POST /api/token/activate` exchanges the JWT + subscribe signature for the `X-Api-Token`
   (`txline_auth.py:99-119`). `acquire_live_credentials` composes the whole flow
   (`txline_auth.py:122-129`).

Every data call then carries `Authorization: Bearer {jwt}` plus `X-Api-Token`
(`veridex/ingest/live_client.py:46-57`). Credentials come from typed config/env only — never the
repo, logs, or events (the `ingest/` package is inside the import-audited trust path, and `httpx`
and `solders` are imported lazily inside the async functions so the trust core stays offline-safe).

### 2.2 The two odds surfaces: SSE stream + full updates history

- **`GET /api/odds/stream` (SSE)** — live StablePrice odds movement. The async shell is
  `stream_marketstates` (`veridex/ingest/live_client.py:134-223`): it parses SSE lines
  (`veridex/ingest/marketstate.py:61-75`), buffers records per `FixtureId`, and folds each batch
  through the one normalizer into a `MarketState` snapshot.
- **`GET /api/odds/updates/{fixtureId}`** — the *full movement history* for a fixture in one call
  (`veridex/ingest/txline_client.py:82-104`). Empirically this returned many days of
  pre-match→in-play history (65k+ updates for one match) even though the OpenAPI describes it as a
  5-minute cache — a documented finding in `docs/txline-feedback.md:21-32`. This endpoint is the
  backbone of both closing-line reconstruction and the real-fixture backtest.
- **`GET /api/odds/snapshot/{fixtureId}?asOf=`** — point-in-time odds, used for historical probes;
  notably it is *empty pre-match*, which is exactly why the closing line must be reconstructed
  from `/odds/updates` rather than snapshotted (CON-040; `veridex/ingest/txline_client.py:7-9`).
- **`GET /api/odds/validation?messageId=`** — TxLINE's own Merkle proof for a sealed odds record
  (`txline_client.py:107-132`), with an honesty guard: odds validate via `validateOdds`, never
  `validateStat`, and an unknown evidence kind raises rather than mislabeling
  (`txline_client.py:53-62`).

### 2.3 Normalization — one projection for live and replay

All TxLINE-native odds messages fold through **one** normalizer,
`marketstate_from_txline_odds` (`veridex/ingest/txline_normalize.py:43-118`), producing the frozen
`MarketState` (`veridex/ingest/marketstate.py:15-29`): `fixture_id`, `tick_seq`, `ts` (max message
`Ts` in ms → seconds), `phase` (1 iff any folded message is `InRunning`), and a `markets` map.

The market key is the composed string
`{SuperOddsType}|{MarketPeriod or ''}|{MarketParameters or ''}`
(`txline_normalize.py:14-21`). This shape matters operationally: real TxLINE totals arrive as
`OVERUNDER_PARTICIPANT_GOALS|…`, *not* `OU|…` — the discrepancy that the real-data run exposed as
a strategy-eligibility bug (§12.3) and that `docs/txline-feedback.md:47-53` asks TxLINE to
document as a canonical `SuperOddsType` table.

Per market, the normalizer extracts:

- `stable_prob_bps` — TxLINE `Pct` × 100, i.e. the de-margined consensus probability in basis
  points (`txline_normalize.py:88-92`). A non-numeric `"NA"` outcome is dropped.
- `stable_price` — decimal odds (TxLINE `Prices` are decimal × 1000; divided by 1000 at
  `txline_normalize.py:96-99`). Prices are retained on suspended markets as last-known odds.
- `suspended` — true iff **no** priced (non-NA) probability outcome remains
  (`txline_normalize.py:103`). This flag is what lets the law *refuse to score* a suspended close
  rather than impute one (§4.1, §12.4).

**"One projection" is doctrine**: replay packs feed raw recorded records through the *same*
normalizer the live stream uses (`veridex/ingest/replay_pack.py:110-133`), so a backtest can never
diverge from live semantics through a second parsing path.

### 2.4 Why StablePrice is the right fair-value input

TxLINE's StablePrice `Pct` is a **de-margined consensus** probability — outcome probabilities sum
to ~100% (verified live; `README.md:110`). That is precisely the input CLV scoring needs:

- CLV is computed *within* this one probability space — entry `stable_prob_bps[side]` vs closing
  `stable_prob_bps[side]` (`veridex/law/recompute.py:151-153`) — so bookmaker margin never
  contaminates the skill metric.
- Because the feed is already de-vigged, Veridex **never re-de-vigs** anywhere — a doctrine stated
  and enforced in the edge module (`veridex/law/edge.py:18-22`): re-de-vigging an already-fair
  probability would manufacture phantom edge.
- The same fair probability doubles as the EV input for forward executable edge at a venue price
  (`p × decimal_price − 1`, `veridex/law/edge.py:30-47`), keeping the entire quantitative
  vocabulary anchored to one consensus fair-value source.

### 2.5 ReplayPacks — tamper-evident recorded market data

A **ReplayPack** (`veridex/ingest/replay_pack.py`) is a self-describing directory:
`pack.json` (manifest) + `odds_<fid>.jsonl` (one raw native record per line) + optional
`updates_<fid>.json`.

- **Provenance from a recorder session.** The capture recorder (`veridex/ingest/recorder.py`)
  writes an append-only `records.jsonl` of enveloped raw records with receipt timestamps, explicit
  `gap` lines for stream gaps (never a silent splice, `recorder.py:40-42`), and crash-safe reads
  (a truncated final line is dropped, `recorder.py:74-86`). `pack_from_session`
  (`replay_pack.py:60-107`) is a pure file transform from session → pack.
- **`content_hash`** — SHA-256 over length-prefixed `(name, bytes)` pairs for each
  manifest-referenced data file in sorted-filename order (`replay_pack.py:41-57`). The hash scope
  equals the `fixtures` manifest exactly: a stale unreferenced file in the directory is excluded;
  length-prefixing makes the decomposition provably injective. `load_pack_marketstates` verifies
  the hash by default and **refuses to replay a tampered pack** (`replay_pack.py:110-133`).
- **Closing policy** is pinned in the pack itself: `closing_policy =
  "con-040_last_pre_inrunning"` (`replay_pack.py:27`) — the closing line is the last
  pre-`InRunning` update, because the pre-match snapshot endpoint is empty.

### 2.6 Closing-line reconstruction (CON-040)

`reconstruct_closing` (`veridex/ingest/txline_client.py:65-79`) walks the updates in order and
returns the last update with falsy `InRunning` — or `None` when the match was already in-running
for every update, in which case **no close exists and none is fabricated**. The live runner
extends this per-market: `_reconstruct_closing_state` (`veridex/runtime/live_runner.py:184-217`)
folds the last pre-`InRunning` update *per market_key* into one closing snapshot (a single update
carries only one market, but the closing snapshot must cover every scored market), then filters to
the window allowlist. Missing coverage triggers an honest degrade, not a guess (§3.4).

### 2.7 Feed health — ops-only, never proof

`FeedHealthReport` (`veridex/ingest/feed_health.py:26-48`) carries source mode, whether
credentials are configured (never the secrets), connection state, last-tick staleness, and tick
counts. Its module docstring states the boundary outright: feed health is **read-only operational
telemetry — never scored, never part of `evidence_hash`, never a proof check, never a leaderboard
input** (`feed_health.py:8-11`). It surfaces in the UI's feed-health panel and gates deploys
(§11.3), but it can never become evidence.

---

## 3. The runtime — one run, N agents, one seal

### 3.1 The core design decision: one run / N agents on identical inputs

A competition is **one run with N agents deciding concurrently on identical inputs**, not N
per-agent runs (`veridex/runtime/orchestrator.py:10-13`). The reason is CLV comparability:

- Every agent decides on the **same frozen tick snapshot** — `CompetitionRun.feed()` gathers all
  agents' decisions concurrently per tick via `asyncio.gather` (`orchestrator.py:352`), each
  wrapped in `asyncio.timeout` and fail-closed (`_decide`, `orchestrator.py:231-252`): a timeout
  or exception becomes an `error` evidence event, never an aborted run.
- Every agent is scored against the **same per-market closing snapshot**, computed once per run
  over the full tick list (`_closing_snapshots`, `orchestrator.py:208-223`) and shared
  (carry-forward 3, `orchestrator.py:19`).

With per-agent runs, an agent could see a different feed, a later tick, or a friendlier close —
and rank differences would be luck of the harness. Here, by construction, rank differences are
strategy.

The async/sync split is equally deliberate (CON-010, `orchestrator.py:3-8`): `feed()` holds all
concurrency; `finalize()` holds the **synchronous** deterministic seal and scoring pass.
Concurrency never reaches the deterministic core, so the sealed output is a pure function of the
inputs.

### 3.2 RunWindow — the coverage window and its sealed config

A live run is framed by a `RunWindow` (`veridex/runtime/window.py:35-77`): `window_id`,
`fixture_id`, a `market_allowlist` (prefix-matched), and an `end_rule` —

- `pre_match` — ends at kickoff; the close is the reconstructed CON-040 close → **true CLV**
  (`clv_bps`);
- `fixed_duration` — ends after `duration_s` (validated present *iff* this rule,
  `window.py:63-77`);
- `manual_stop` — ends on demand.

Two honesty rules attach to the window:

1. **`clv_field_name`** (`window.py:80-87`): only a `pre_match` window yields true CLV; the other
   rules close on the line *at window end*, which is **window-CLV** and is named `window_clv_bps`
   everywhere — the row's numeric value is physically renamed at finalize
   (`row[clv_field] = row.pop("clv_bps")`, `orchestrator.py:536-540`) so downstream can never
   mistake one for the other.
2. **`is_pending_horizon`** (`window.py:90-106`): an action entered strictly within
   `min_clv_horizon_s` of window close has too little runway to earn a meaningful closing move, so
   it is excluded from CLV means via the existing `"pending"` sentinel — exactly like WAIT, never
   a numeric 0.

**Why the window config is SEALED.** At finalize, a windowed run emits one `window_config`
RunEvent carrying `{end_rule, min_clv_horizon_s, window_end_ts}` *inside* the evidence-hash-covered
prefix (`orchestrator.py:461-482`; the event-type constant lives in the pure window module,
`window.py:32`, so writer and verifier can never drift). This closes a concrete attack: score rows
are *not* evidence-hashed (§5.1), so an attacker could relabel a genuinely-scored losing row as
`reason="pending_horizon"`, `clv_bps="pending"` — hiding a loser from the CLV mean while
`evidence_integrity` stays green. With the window config sealed, `METRICS_RECOMPUTED` re-derives
`is_pending_horizon(entry_ts, window_end_ts, min_clv_horizon_s)` from sealed data and **fails**
any row whose label doesn't genuinely qualify — or whose label isn't derivable because no window
config was sealed (`veridex/checks/build.py:251-272`). The relabel evasion is pinned by
`tests/test_checks_integrity.py:490` (`test_relabel_evasion_is_caught`).

### 3.3 The event model — sealed prefix vs derived tail

There are **three event tiers**, and the boundaries between them are the trust architecture:

1. **Sealed `RunEvent`s** (`veridex/runtime/schemas.py:37-46`) — `tick`, `decision`, `error`, and
   (windowed runs) `window_config`, each with a unique integer `sequence_no`. These are validated
   through the `RunEvent` schema *before* hashing (`validate_run_events`,
   `orchestrator.py:187-200`) and form the evidence-hash-covered prefix. Duplicate sequence
   numbers are a hard error (`veridex/runtime/evidence.py:28-33`) — ambiguous sort order would be
   a determinism hole.
2. **Derived `CompetitionEvent`s** (`veridex/competition/events.py:104-169`) — the canonical
   spectator log. Every event carries an explicit `evidence` boolean. Evidence events (seq =
   `sequence_no + 1`, `events.py:543`) are hash-bound to the FULL sealed RunEvent via
   `payload_hash` (`events.py:551` — the hash covers the sealed record, not the smaller UI
   payload). Derived events — `LAW_RESULT`, `SCORE_UPDATE`, `PROOF_ANCHOR`, `POLICY_RESULT`,
   `EXECUTION_SUBMITTED`, `EXECUTION_RECEIPT`, `APPROVAL_AUDIT`, `EXECUTION_ROUTE`,
   `COMPETITION_FINALIZED` — are all `evidence=False` with non-empty `derived_from` references.
   **Scores, receipts, and route/telemetry events are structurally OUTSIDE the evidence hash.**
3. **`RuntimeEvent` ops telemetry** (`veridex/runtime/runtime_events.py`) — model calls, latency,
   tokens, status. It is made *structurally impossible* to seal: a RuntimeEvent carries **no
   `sequence_no`, no `evidence` flag, no `payload_hash`** — the three fields the evidence path
   requires — so it cannot masquerade as a RunEvent or CompetitionEvent
   (`runtime_events.py:1-13`). Its sink type is distinct from the orchestrator's evidence sink
   (`runtime_events.py:90-92`).

The **live spectator stream is a projection, never a second source of truth**: the seq-0
`COMPETITION_STARTED` event and each per-RunEvent evidence event are built by shared constructors
that both the offline projection (`build_event_log`, `events.py:556`) and the live sink
(`veridex/competition/service.py:387-401`) call — live ≡ projection by construction — and
finalize *verifies* the persisted live prefix is canonically byte-equivalent to the projection
prefix before appending only the derived tail (`service.py:417-427`,
`_assert_prefix_parity` at `service.py:564-592`).

### 3.4 The live runner — stream → seal → close → finalize → proof

`run_live_window` (`veridex/runtime/live_runner.py:225-461`) is the async shell for one windowed
live run:

1. **Stream in real time.** Ticks are fed into `CompetitionRun.feed()` *as they arrive* — never
   buffered-then-replayed, which would be replay dressed as live (`live_runner.py:4-5`). Fixture
   filtering, allowlist filtering (`_filter_markets`, `live_runner.py:171-181`), and the end-rule
   checks happen per tick. For `manual_stop`, the next-tick await is *raced* against the stop
   event (`_race_next_tick`, `live_runner.py:98-119`) so a stop is honored promptly even on an
   idle stream (halftime, no line moves) instead of hanging until some future tick.
2. **Seal the authoritative close.** For a `pre_match` window, the first in-running tick
   terminates the window *without being fed* (it is the line agents are scored against, not a
   decision point — `live_runner.py:314-318`); the runner then fetches `/odds/updates` and
   reconstructs the CON-040 close, sealing it via `feed_closing` **before** `finalize`
   (`live_runner.py:363-400`; `CompetitionRun.feed_closing` at `orchestrator.py:381-409` emits a
   normal TICK evidence event and gathers no decisions). The verifier therefore recomputes true
   CLV from the authoritative close, not from the last stream tick a gap could have made stale.
3. **Honest degrade, never a fabricated close.** Three triggers degrade a `pre_match` run to an
   effective `manual_stop` (window-CLV, never true CLV): the close fetch raised
   (`live_runner.py:375-378`), no pre-in-running update exists (`live_runner.py:401-407`), or the
   reconstructed close doesn't cover every market seen during the window — the completeness gate
   at `live_runner.py:385-399` (coverage is checked against `seen_markets`, a conservative
   superset of the scored set). Each degrade records a **non-sealed** ops marker
   (`closing_source: "stream_observed_fallback"`, `live_runner.py:58`) — stream-observed CLV is
   never presented as true CLV.
4. **Honest interrupt degrade.** If the stream raises mid-window after at least one tick was fed,
   the partial run is *finalized as a degrade* and returned — hours of sealed evidence are not
   vaporized, and the interruption is explicit in `ops["stream_interrupted"]`
   (`live_runner.py:339-358`). Zero ticks fed → the error re-raises (nothing to seal).
5. **Proof only after the seal.** `finalize` runs (`live_runner.py:410`), then — and only then —
   scores, manifest, anchor, checks, and the proof card are composed
   (`live_runner.py:417-451`). The proof is downstream of the seal by construction (DEC-2D-3).

### 3.5 Persist-then-broadcast — a crash can't strand unproven claims

Wherever sealed events are streamed live, the store append **must complete before** the broadcast
is attempted: the competition service's live sink appends each evidence event, then broadcasts
(`veridex/competition/service.py:395-401`), and broadcast errors are swallowed so a dead spectator
never aborts the run (`_safe_broadcast`, `service.py:556-561`). A spectator can therefore never
see an event that isn't durably persisted, and a crash between persist and broadcast leaves the
persisted record authoritative. Pinned by `tests/test_evidence_broadcast.py:134`
(`test_persist_happens_before_broadcast_for_every_evidence_event`) and neighbors (ordered
broadcast, raising-broadcast isolation, no-callback byte-identity).

### 3.6 Sequencing discipline

- Sealed `sequence_no` is a strictly increasing integer assigned by the run
  (`orchestrator.py:310, 350, 379`); the hash function rejects duplicates
  (`evidence.py:28-33`).
- Canonical `seq` is `sequence_no + 1` for evidence events (seq 0 is `COMPETITION_STARTED`), and
  the derived tail continues strictly after (`events.py:543, 610`).
- The execution block (§6.4) appends as a second derived block with seq strictly after the 2A
  tail, explicitly excluded from the prefix-parity check (`service.py:429-434`).
- The frontend WebSocket client treats a sequence gap or slow-client overflow as a **disconnect +
  resync from the persisted log**, never a silent drop (`apps/web/lib/ws.ts:1-5`).

---

## 4. The law and scoring

### 4.1 `law/recompute.py` — the deterministic law

`recompute(entry_state, action, *, closing, source_mode)` (`veridex/law/recompute.py:102-165`) is
the trust core the entire leaderboard ranks on. Its contract:

```
clv_bps = closing.stable_prob_bps[side] − entry.stable_prob_bps[side]     # de-vigged prob, bps
```

Key properties, each visible in the source:

- **It never trusts agent numbers.** The claimed edge is read out of `action.params` *only to be
  recorded as untrusted metadata* via `compute_clv_check` (`recompute.py:155-157`;
  `veridex/checks/clv.py:29-49` scores on `recomputed_edge_bps` only and stores
  `claimed_edge_bps_untrusted` in a rules block). Two code paths, no flow between them.
- **WAIT is a valid abstention, never scored** — `clv_bps == "pending"`, `valid=True`, reason
  `wait_unscored` (`recompute.py:115-117`). The non-numeric sentinel means the scorer *excludes*
  it from CLV means rather than counting a 0.
- **Replay vs live semantics are explicit.** `source_mode` is validated at the boundary
  (`recompute.py:110-113` — a misspelling raises loudly rather than silently falling through to
  replay semantics, which would wrongly invalidate a live not-yet-closed action). In replay a
  missing closing is *invalid* (`closing_missing` — the fixture is complete, there is no future
  tick to wait for); in live it is *pending* (`pending_closing`, `recompute.py:135-145`).
- **Invalidity ordering is fixed**: absent → suspended → side-missing, entry validated before
  closing so the most upstream failure wins the reason code (`_validate_market`,
  `recompute.py:86-99`). This is where `closing_suspended` comes from — the reason that made the
  real-data run's two fires unscoreable (§12.4): the law **refuses to fabricate a closing price**
  for a suspended line.
- **Kelly is advisory only.** `_kelly_fraction` (`recompute.py:57-78`) computes
  `f* = (b·p − q)/b` clamped to [0,1] at entry from the sealed de-vigged prob + decimal price. It
  never gates validity and never enters any rank key; downstream it only sizes stakes under the
  policy cap (§6.4).
- **Phase-1 edge == CLV.** With no independent fair-value source beyond TxLINE, the only
  evidence-derived edge *is* the closing-line value (`recompute.py:22-25, 160`). The forward,
  venue-price-dependent quantity is a different function in a different module —
  `executable_edge_bps(prob_bps, executable_price) = round((p·price − 1)·10000)`
  (`veridex/law/edge.py:30-47`) — and it gates execution, never scoring (§6.2, §7).

The module is on the audited trust path: no LLM SDK import, enforced statically (§5.3.2).

### 4.2 `is_scored` — the single source of truth for "did this row score?"

```python
row.get("valid") is True and isinstance(row.get("clv_bps"), int) and not isinstance(row.get("clv_bps"), bool)
```

(`veridex/scoring.py:51-69`.) Three details are load-bearing:

- The **bool guard**: `isinstance(True, int)` is `True` in Python, so without it a stray boolean
  `clv_bps` could masquerade as a numeric score.
- The `"pending"` sentinel (WAIT, live-pending, pending_horizon) is a `str` and is excluded
  *without ever pattern-matching `reason`* — scoring keys on `valid` + numeric `clv_bps` only.
- It is imported by everything that needs the predicate — the orchestrator's windowed overrides
  (`orchestrator.py:525-529`), the event-log projection (`events.py:663`), the backtest report
  (`report.py:196`) — so the "scored set" can never silently desync across surfaces. Its window
  twin `is_window_scored` (`scoring.py:72-95`) keys on the distinct `window_clv_bps` field; the
  two are mutually exclusive per row *by construction* because finalize renames the value
  (`scoring.py:81-86`).

### 4.3 The metric stack and the CLV-only rank key

`score_run` (`scoring.py:241-273`) produces one row per participating agent — including agents
with zero scored actions (`avg_clv_bps=None`, ranked last). The stack: pooled `avg_clv_bps`,
`total_clv_bps`, `sim_pnl` (an honestly-labeled closing-referenced flat-stake *proxy*, equal to
total CLV — its real job is to define the series `max_drawdown` reads from,
`scoring.py:19-26`), Brier (only when the agent emitted a usable numeric confidence;
closing-line direction as the outcome indicator, `scoring.py:142-163`), `max_drawdown` (≤ 0,
`scoring.py:120-139`), `action_count` (scored picks), `valid_pct`/`valid_count`
(law-acceptance — a *distinct* metric that includes valid WAIT abstentions,
`scoring.py:193-197`), and the labeled window-CLV aggregate.

The rank key (`_rank_key`, `scoring.py:217-238`) is: avg CLV desc (None last) → total CLV desc →
Brier asc → drawdown desc → action count desc → agent_id (deterministic tiebreak). The cross-run
leaderboard uses the same ordering (`veridex/leaderboard.py:203-229`) with pooled
`avg_clv_bps = Σtotal / Σcount` (a true mean over scored actions, not a mean-of-run-means,
`leaderboard.py:147-148`). **Confidence, Kelly, eligibility badges, and anchor status are
deliberately absent from both rank keys** — proof completeness is an eligibility badge, not a
performance score (`leaderboard.py:210-211`).

### 4.4 Window-CLV: a separate, labeled metric — never blended

A `fixed_duration`/`manual_stop` run's scored rows carry `window_clv_bps` *instead of* `clv_bps`
(§3.2). The aggregation honors that everywhere: `score_run` populates
`avg_window_clv_bps`/`total_window_clv_bps`/`window_action_count` as a labeled supporting
aggregate that is never folded into `avg_clv_bps` and never enters `_rank_key`
(`scoring.py:184-191, 210-212`); the leaderboard pools it the same way
(`leaderboard.py:150-158, 191-194`); the proof card surfaces it as a distinct `window_clv` metric
beside `clv` (`veridex/checks/build.py:518-558`); the event-log projection carries
`mean_window_clv_bps` beside `mean_clv_bps` (`events.py:667-687`); and the UI glossary defines it
as "never shown as CLV" (`apps/web/lib/glossary.ts:65-68`).

### 4.5 `clv_confidence` — and the bug the real-data run caught

`clv_confidence(n)` (`veridex/clv_confidence.py:21-39`) maps a sample size to a display-only tier:
≤9 → "low", ≤29 → "medium", else "high" — flagged, never reordered (it never enters a rank key).

**The bug story.** The tier was originally keyed off `valid_count` — *law-valid decisions*. But a
valid WAIT is law-valid, so an agent that abstained a thousand times and scored **zero picks**
would read "high confidence." The real-data run (§12) produced exactly that shape — 0 scored
picks — and the overclaim became visible. The fix keys the tier off the **scored-pick count** on
both surfaces:

- Leaderboard: `clv_confidence(action_count)` — with `valid_count` kept exposed as the distinct
  law-acceptance sample and an explicit comment stating the rule
  (`veridex/leaderboard.py:196-199`).
- Backtest report: `clv_confidence(len(scored_clv))`, with the comment naming the old behavior an
  overclaim (`veridex/backtest/report.py:299-304`).

The report also separates the populations explicitly: `sample_size` is total decisions
(WAIT-inclusive, the `law_valid_rate` denominator), `clv_distribution.count` is the scored-pick
count and the confidence basis (`report.py:153-163`). `law_valid_rate` exists precisely so
law-acceptance has an honest name of its own — and `policy_pass_fail_rate` stays `None` until a
real policy-envelope evaluation backs it, rather than a law-validity number wearing a policy name
(`report.py:173-183, 320-324`).

---

## 5. The proof layer

### 5.1 `evidence_hash` — SHA-256 over the canonical sealed prefix

`compute_evidence_hash` (`veridex/runtime/evidence.py:19-36`) sorts the validated RunEvents by
`sequence_no`, rejects duplicate sequence numbers, canonically serializes the whole array
(`serialize_payload` — sorted keys, compact separators, `evidence.py:14-16` — the ONE canonical
serializer reused by every hash in the system), and takes SHA-256 of the bytes. Hashing the whole
sorted array (rather than concatenating per-event strings) makes the wire format unambiguous.

**`run_id` is deliberately NOT in the evidence hash.** The hash input is the event list alone.
Consequence: **identical inputs ⇒ identical evidence bytes**, regardless of which run minted them.
This is not an accident — it is provable in the suite: a paper run and a dry-run over identical
fresh streams (different `run_id`s) produce byte-identical `proof_card["evidence"]` blocks and
identical score stacks (`tests/test_standalone_run.py:220-256`,
`test_standalone_dry_run_emits_nonscoring_receipts_leaving_proof_unchanged`). run_id-independence
is what makes "re-run it yourself and compare hashes" a meaningful third-party verification move.

What IS bound to the run identity is the **manifest**: `run_manifest`
(`veridex/chain/anchor.py:13-32`) binds `run_id`, `fixture_or_window_id`, `agent_ids`,
`action_evidence_root` (= the evidence hash), `score_root`, `proof_mode_map`, and schema versions;
`run_manifest_hash` (`anchor.py:35-42`) is its canonical SHA-256 — and the exact anchored payload.

The scoring pass reinforces the ordering "evidence before score": every score row binds a
`raw_prescore` record whose hash covers `{evidence_hash, raw_action, schema_version, agent_id,
config_hash, tick_seq, proof_mode}` (`evidence.py:39-67`), and the score row derives only from
that bound hash + recomputed values (`evidence.py:70-77`; wired at `orchestrator.py:500-512`).

### 5.2 The seven checks — each recomputes, each can FAIL

The check taxonomy is a frozen 7-member enum (`veridex/checks/result.py:22-31`) with fixed labels
and severities (`result.py:35-55`; ANCHOR is `info` so an offline replay's honest
`not_applicable` anchor never reads as a blocking failure). CLV is deliberately **not** a CheckId:
checks certify the record; metrics rank performance (SEC-001). Every verdict is
`pass | fail | pending | not_applicable` — never a hardcoded PASS — and every builder fails
closed on an exception (`veridex/checks/build.py`).

| Check | What it recomputes | How it FAILS |
|---|---|---|
| `evidence_integrity` | Recomputes `compute_evidence_hash(run.run_events)` and compares to the sealed hash (`build.py:68-89`) | Any altered sealed byte; any malformed/unrecomputable evidence (exception → fail, never a crash) |
| `llm_boundary` | Re-runs the static AST import audit over the seven trust targets — `law/`, `scoring.py`, `leaderboard.py`, `verifier/`, `checks/`, `ingest/`, `policy/` (`build.py:40-49, 92-113`) | Any forbidden LLM SDK import (`agno`, `anthropic`, `openai`, `google.generativeai`, `litellm` — `veridex/verifier/import_audit.py:12`); **or a missing trust directory** — each target's existence is asserted before auditing, because `rglob` over a deleted dir would vacuously pass (fail-open inverted to fail-closed, `build.py:96-104`) |
| `metrics_recomputed` | Re-derives every persisted row's CLV by re-running the law over MarketStates reconstructed from the sealed tick snapshots and the action read from the sealed decision event — never from the row itself (`build.py:182-318`) | A doctored `clv_bps` (or `window_clv_bps` — the check verifies whichever field the row actually carries, `build.py:277-291`); a coordinated row tamper (action sourced from sealed events closes that evasion, `build.py:145-164`); a fake `pending_horizon` relabel (§3.2, `build.py:251-272`); a displayed aggregate diverging from the rows (`build.py:293-297`) |
| `manifest_bound` | Verifies the manifest binds the same run_id / evidence root / score root / proof-mode map, and that `run_manifest_hash(manifest)` equals the anchored hash (`build.py:321-367`) | Any unbound field; a malformed manifest fails closed |
| `policy_obeyed` | Correlates every `denied` `policy_result` (by `execution_id`) with any `execution_submitted` for the same execution (`build.py:406-445`) | A submit that bypassed a deny |
| `receipt_separation` | Audits that every executor-lane event (`policy_result`, `execution_submitted`, `execution_receipt`, `approval_audit`) stayed `evidence=False` (`build.py:448-475`) | Any receipt that leaked into the sealed evidence flag |
| `anchor` | Reads the anchor block: anchored+signature → pass; pending → pending; unanchored → `not_applicable` for offline replay, `pending` for live (`build.py:370-386`) | Honest by state; severity `info` |

**The hardening pass.** Three of these checks originally *structurally couldn't fail* and were
fixed: `llm_boundary` would pass vacuously on a missing trust dir (now raises → fail);
`metrics_recomputed` originally verified rows against inputs that a coordinated tamper could edit
alongside the score (now both recompute inputs come only from the hash-protected `run_events`,
`build.py:186-201`); and `manifest_bound`/`policy_obeyed`/`receipt_separation` were placeholders
before Tasks 3–4 wired their real verdicts (`build.py:6-9`). The suite tampers on purpose — a
doctored `clv_bps` fails `metrics_recomputed` even with an intact seal
(`tests/test_checks_integrity.py`), and the manifest check is falsifiable end-to-end
(`tests/test_standalone_run.py:161`).

### 5.3 Manifest, Merkle root-forest, and the Solana anchor

- **Root-forest.** `build_root_forest` (`veridex/chain/merkle.py:53-82`) computes per-domain
  Merkle roots — `event_log`, `score`, `receipt`, `policy`, `competition`, `payout_reserved` —
  each a deterministic binary Merkle tree over canonically-hashed leaves (`merkle.py:19-50`,
  empty domain → a distinct `EMPTY_ROOT`). The forest is attached to the manifest before hashing
  (`veridex/verifier/recompute.py:109-138, 215`), so the anchored commitment binds sealed records
  per domain, not just one flat hash. Receipts never enter the manifest binding on the demo path
  (`recompute.py:129` — receipts domain empty; SEC-004).
- **Anchor.** `anchor_memo` (`veridex/chain/anchor.py:75-162`) sends ONE SPL Memo transaction on
  Solana devnet whose `data` is *verbatim* the 64-hex-char manifest hash — validated before any
  I/O. One Memo per run, not per tick. Offline runs are honest: `anchor_status="not_anchored"`,
  the ANCHOR check reads `not_applicable` (replay) or `pending` (live), and the explorer URL
  helper returns `None` rather than a dead link (`anchor.py:53-72`). A real anchored run confirmed
  in ~1.3 s on devnet (`README.md:108-110`).

### 5.4 `POST /runs/{id}/verify` — recompute fresh, report honestly

The verify endpoint (`veridex/api/router.py:617-683`) loads the sealed run from the store and
delegates to the trust-path core `verify_run` (`veridex/verifier/recompute.py:171-233`), which:

1. recomputes the evidence hash over the sealed prefix and compares (`evidence_match`);
2. **re-derives the ranked score rows fresh** via `score_run` — it never echoes persisted scores,
   which is what catches a doctored score row even when the seal is intact;
3. rebuilds the score root, the root forest, and the manifest through the *same* helpers the seal
   path used (`manifest_from_run` / `root_forest_for_run` are the single source of those formulas,
   `recompute.py:10-14`), so the reconstructed `manifest_hash` is byte-identical to the anchored
   Memo payload;
4. fails closed: a malformed/tampered run that raises anywhere yields a structured
   `verified=False` report with the error captured — never a 500 on the flagship endpoint
   (`recompute.py:217-219`).

The response semantics are deliberately layered: top-level `verified` reflects **evidence-prefix
integrity** (`recompute.py:232`); the per-check block carries the **full verdict** (all 7 checks
rebuilt fresh, `router.py:653-661`). The frontend renders "⚠ NOT fully verified" when a blocking
check fails even with an intact seal — no false green. Deployed agents, arena runs, backtests, and
the demo all verify through this one endpoint (one flow to proof, §11.6).

The read-only Proof Explainer (`router.py:687+`) narrates an already-produced proof via an LLM but
receives only a sanitized read-model (served response fields + the pinned glossary — never the raw
RunResult or store handle), and the explainer package is *reverse* import-audited: it may import
nothing from the trust path (`assert_no_trust_imports`,
`veridex/verifier/import_audit.py:80-104`).

### 5.5 Receipts are non-scoring — and provably causally inert

The doctrine has three enforcement layers:

1. **Structural**: executor-lane events are built `evidence=False` with `derived_from` refs by
   dedicated constructors that `build_event_log` never calls
   (`veridex/competition/events.py:273-490`).
2. **Checked**: `receipt_separation` fails if any leaked (§5.2).
3. **Proven causally inert**: running the execution lane produces score stacks and evidence blocks
   byte-identical to not running it — `tests/test_standalone_run.py:220-256` (dry vs paper) and
   `tests/test_execution_integration.py:165` (proof-card skill block byte-identical with fills).
   The lane reads only sealed `score_rows` and never mutates the RunResult
   (`veridex/execution/runner.py:5-11, 249-250`).

### 5.6 The golden byte-for-byte suite — the seal-safety net

Before the incremental-runtime refactor, the then-current `run_competition` output was pinned as
two committed JSON fixtures — a happy path and a concurrent-error/timeout path — produced by a
deterministic generator (`tests/golden/generate_golden.py`; fixed `run_id`, deterministic agents,
env-independent timeouts, stable exception strings). The pin test is four lines:
`run_case_dump(case) + "\n" == golden_text` (`tests/test_orchestrator_golden.py:17-19`). The
generator itself refuses to write a fixture if two consecutive runs differ
(`generate_golden.py:127-135`).

This is what made the entire Phase-2D rebuild safe: the batch loop was extracted into
`CompetitionRun.feed()/finalize()`, the live runner, windowed scoring, the event sink, and the
execution lane were all added — and at every step the sealed bytes were provably identical to the
pre-refactor baseline (window=None and sink=None paths are byte-identical by construction,
`orchestrator.py:279-283, 446-447`). When a later change *should* alter sealed bytes, the golden
must be regenerated deliberately — which is precisely why Sharp Momentum v2 was wired additively
rather than by renaming v1 (§10.6).

---

## 6. Policy and execution safety

### 6.1 The PolicyEnvelope

The `PolicyEnvelope` (`veridex/policy/envelope.py:18-65`) is the operator's committed guardrail
set: `max_stake`, per-run/session/day order caps, venue and market allowlists, `min_edge_bps`,
`max_slippage_bps`, `max_price`, `max_quote_age_s`, `cooldown_s`, `human_approval_threshold`, the
live-only `max_stake_live_guarded`, `circuit_breaker_threshold`, and `kill_switch`. Its
`policy_hash()` uses the one canonical serializer, so the commitment is byte-stable and directly
comparable to the rest of the evidence chain; it is recorded on every emitted `POLICY_RESULT`
event, binding the persisted execution block to the exact envelope that governed it
(`veridex/competition/service.py:498-502`).

### 6.2 The two-phase gate — deny before you pay for a quote

The gate is split around the venue quote (`veridex/policy/gate.py`):

- **Pre-quote** (`evaluate_pre_quote`, `gate.py:96-125`): kill-switch, circuit-breaker state,
  sealed-edge pre-screen, stake cap, live-only stake cap, venue allowlist, market allowlist,
  per-run order cap, cooldown, agent eligibility — all cheap, deterministic facts, all checked
  **before any venue I/O**. A pre-quote deny emits a `phase="pre_quote"` POLICY_RESULT and skips
  the quote entirely (`veridex/execution/runner.py:334-358`).
- **Post-quote** (`evaluate_post_quote`, `gate.py:128-154`): quote staleness, book-depth
  liquidity (`quoted_size < stake` denies), *real* slippage against the sealed reference price,
  forward `executable_edge_bps` at the actual quoted price, max price, stake — the
  price-dependent facts that only exist once a quote does. A clean pass at or above
  `human_approval_threshold` escalates to `REQUIRES_HUMAN` instead of auto-approving.

**Why two phases.** (1) Don't pay venue latency — or reveal intent to a venue — for an order that
deterministic policy would kill anyway. (2) The split *fixed a real inert-gate bug*: the earlier
single-pass engine was fed `slippage_bps=0` by the runner, so the slippage rule could never fire
(`gate.py:5-8`). Post-quote now receives slippage computed from the actual quote vs the sealed
entry price (`runner.py:186-203, 365`) and the forward edge at the actual price (`runner.py:366`).

Both phases collect **all** failing reason codes (never short-circuit) from a single named-literal
vocabulary (`veridex/policy/engine.py:18-35`), so a deny always explains itself completely.

### 6.3 The circuit breaker — a pure, frozen state machine

`CircuitBreaker` (`veridex/policy/circuit_breaker.py:41-139`) is an **immutable** (frozen
pydantic) CLOSED/OPEN/HALF_OPEN state machine. Every transition returns a *new* instance —
`record_failure` (opens at `threshold` consecutive failures, anchoring the cooldown at the
injected `now`), `record_success` (a HALF_OPEN probe success fully closes), `resolve` (the
time-based OPEN→HALF_OPEN recovery — time is always injected, never a wall clock), `start_probe`
(HALF_OPEN admits exactly one probe). The same event sequence always yields the same state
(`tests/test_circuit_breaker.py:100`), which makes breaker behavior replayable and testable
without clocks or sleeps. The mutable bit lives in one place: `BreakerCell`
(`veridex/execution/runner.py:82-100`) threads the current immutable state through a lane run,
reassigning it on each transition — a mid-lane trip therefore blocks all remaining proposals.

**Single authority.** The breaker never decides anything on its own, and neither does the runner:
the policy gate alone reads `breaker.allows()` and mints the `circuit_open` deny reason
(`gate.py:102-105`). The runner only *constructs and threads* the cell and updates it around
**executed** outcomes — a real live fill/failure; a policy denial never reaches a receipt and so
can never trip the breaker (`runner.py:62-73, 454-464`). This is enforced by a source-level test
that greps the runner: `".allows(" not in src` and `"circuit_open" not in src`
(`tests/test_policy_gate.py:155-165`, `test_runner_has_no_second_authority`). There is exactly one
gate, and it cannot silently grow a shadow twin.

### 6.4 The execution lane — strictly downstream of the seal

`run_execution_lane` (`veridex/execution/runner.py:206-501`) turns a **sealed** run into
policy-gated execution attempts:

1. **Proposal selection is sealed-only.** `value_proposals`
   (`veridex/strategies/value.py:110-177`) selects rows with `valid is True` and law
   `recomputed_edge_bps ≥ min_edge_bps`; `market_key`/`side` come from the sealed action bytes,
   the sealed entry price/probability from the sealed tick snapshot, `kelly_fraction` from the
   sealed law output. It never reads a claimed edge, a confidence, or a venue quote as fair value.
2. **Stake sizing is half-Kelly under the cap** (`_size_stake`, `runner.py:162-183`):
   `0.5 × kelly × bankroll`, capped at `max_stake`, NaN-guarded, with a small deterministic
   fallback when the law advised no sizing. Sizing is policy execution only — never a metric.
3. **Quote-size coupling.** The quote is priced `for_size=stake` — the *same variable* that later
   constructs the `Order` (`runner.py:362-364, 426-432`). The slippage and executable edge the
   post-quote gate acts on therefore reflect exactly the size that submits; there is no
   gate-approves-one-size-fills-another gap. Pinned by
   `tests/test_execution_safety_gates.py:154` (`test_quote_priced_for_the_size_that_submits`).
4. **Mode branching**: `DENIED` → record rejected, no submit; `REQUIRES_HUMAN` → parked
   `awaiting_human` for the operator-audited resolution path (`resolve_approval`,
   `runner.py:509-733`, which re-checks law + the *current* envelope + eligibility before any
   submit and always emits a non-scoring approval-audit event); `paper` → no submit at all;
   `dry_run` → a simulated receipt built directly, never touching the submit path
   (`runner.py:434-442`); `live_guarded` → real `submit_order` → poll-until-terminal → honest
   receipt (§8.2).
5. **Lifecycle legality.** Every record walks the explicit transition table
   (`veridex/execution/models.py:48-73`); an illegal skip/backward transition raises
   (`models.py:134-155`).

**Failure isolation.** In the standalone runner the lane runs strictly after seal + verify +
anchor, inside a guard: a lane exception (venue down, unwired adapter) returns the intact — 
possibly already anchored on-chain — proof with `receipts=[]` and the cause recorded honestly in
the non-sealed `ops` channel (`veridex_agent/run.py:414-432`; pinned by
`tests/test_standalone_run.py:284`, `test_standalone_lane_failure_preserves_sealed_proof`). An
execution failure can degrade execution; it can never vaporize a proof.

---

## 7. Price-unit and display honesty

### 7.1 The price-unit doctrine: Veridex speaks decimal odds

Venues price in incompatible native units — Polymarket's book is denominated in share prices
`q ∈ (0,1)`; other books quote implied odds. The trust core consumes exactly one unit: **decimal
odds** (`veridex/law/edge.py` computes `p·price − 1`; the policy gate and the UI read the same
unit). So the doctrine is: adapters own native↔decimal conversion at the venue boundary, exactly
once, and the native value survives only as an **audit** field.

This is made **structural** in the quote contract (`veridex/venues/base.py:43-69`): `Quote.price`
is decimal odds — the executable cost-to-fill for `for_size`, not a midpoint — and
`Quote.native_price` is the venue-native value it derived from, audit-only. There is no ambiguous
`.price` whose unit depends on the venue. The same split exists on `OrderStatus`
(`base.py:112-131`) and in the book levels (`QuoteLevel.native_price`, raw depth, never an edge
input, `base.py:27-41`). On the write side the inversion happens once at submit:
`round_to_tick(1/order.price)` — a decimal-odds value never reaches the wire
(`veridex/venues/polymarket.py:530-548`, §8.2). A native `q` leaking into `.price` would silently
corrupt every downstream edge/slippage/policy number (`polymarket.py:14-17`) — the structural
split is what makes that class of bug unrepresentable rather than merely unlikely.

### 7.2 `real_venue_quote` is EARNED, never inferred

The POLICY_RESULT payload's `real_venue_quote` flag is set from exactly one input: an explicit
class marker, `PROVIDES_REAL_VENUE_QUOTE = True`, that only a genuine real-venue adapter declares
(`veridex/venues/polymarket.py:350-354`); the lane reads it fail-closed via
`_is_real_venue_quote` (`veridex/execution/runner.py:75-110`). It is **never inferred from the
presence of a number**: the offline `FakeVenueAdapter` returns a perfectly plausible fixed 2.05
decimal price, and its quotes still read `real_venue_quote: false`
(`tests/test_execution_safety_gates.py:260-281`). A future adapter that wants the flag must
*declare* it — and thereby step into the armed-path expectations that come with it (§9).

### 7.3 The frontend display gate

An edge number renders **only** when a genuine venue quote backs it:
`hasRealVenueQuote(s) = s.real_venue_quote === true && venue_decimal_price != null &&
executable_edge_bps != null` (`apps/web/lib/edge-gate.ts:16-18`). The gate keys on the
real-quote *signal*, never merely "a number is non-null" — a Fake/paper quote's numbers must
never be presented as edge (`edge-gate.ts:1-7`). Fail-closed: any missing piece ⇒ no edge shown.

### 7.4 Mispricing gap vs executable edge — two spaces, kept apart

- **Mispricing gap** (`mispricing_gap_bps`, `veridex/execution/legibility.py:41-57`) =
  `fair_prob_bps − round(10000/venue_decimal_price)` — a *probability-space dislocation*.
  Explanatory only; never labeled "edge," never scored.
- **Executable edge** (`veridex/law/edge.py:30-47`) = `round((p·price − 1)·10000)` — an
  *expected-value* quantity at the actual venue price, for the size that submits. It gates
  execution; it is never a score.

At the fair decimal price `1/p` both are exactly 0 — a consistency property that holds only
because `prob_bps` is already de-margined and never re-vigged (`legibility.py:15-18`). Both travel
together on the post-quote POLICY_RESULT payload (`runner.py:395-412`) and are defined distinctly
in the single-source UI glossary (`apps/web/lib/glossary.ts:13-19`), which screens must pull from
verbatim.

### 7.5 Provenance travels WITH numbers

The judge demo's data provenance is machine-readable at every hop
(`scripts/demo_phase2d.py`):

- The shipped synthetic pack **self-labels** in its own manifest: `capture.synthetic: true` +
  `capture.provenance: "synthetic-illustrative"` (`demo_phase2d.py:143-148`) — stamped into
  `pack.json`, outside the data-file `content_hash` scope so run ids stay stable.
- `_pack_provenance` (`demo_phase2d.py:152-181`) reads the label **coherently and fail-safe**:
  synthetic is derived from the bool OR the provenance string (the two can never disagree in the
  caveat-dropping direction); a positively-stamped non-synthetic pack reads as captured odds —
  still "a paper/backtest signal, NOT a live-executed real-money edge"; and an **unmarked pack
  reads `"unknown-provenance"`** with a cautious caveat — it is never silently promoted to
  "real"/"captured" (`demo_phase2d.py:163-165, 180-181`).
- The resulting caveat rides **inline with every CLV number** on all three surfaces — manifest run
  entry, printed console summary, pack capture block (`demo_phase2d.py:16-22, 68-74`) — so a demo
  CLV can never be quoted apart from the fact that its odds are illustrative.

---

## 8. Venues — the adapter seam and Polymarket

### 8.1 The VenueAdapter seam

`VenueAdapter` (`veridex/venues/base.py:271-355`) is a structural Protocol: four async I/O
methods — `quote_market(market_ref, for_size)`, `submit_order`, `get_order_status`,
`cancel_order` — plus the sync, pure `normalize_receipt` bridge into the trust-path
`ExecutionReceipt`. HTTP imports must be lazy inside the async methods so importing any adapter
module is offline-safe.

**Honest fill semantics** are baked into the seam, not left to adapters:

- `Order.tif` is `Literal["FAK", "FOK"]` — **GTC is unrepresentable** for this lane
  (`base.py:72-97`); `client_order_id` is a required idempotency identity.
- `poll_order_terminal` (`base.py:187-225`) polls until a *terminal* native status or timeout, and
  on timeout returns the **last observed status unchanged — it never fabricates a fill**. A
  non-terminal status surviving to the receipt boundary maps to `UNRESOLVED`
  (`map_venue_status`, `base.py:150-184`) — the honest label for "we don't know," and an
  UNRESOLVED live receipt counts as an executed *failure* for the breaker
  (`veridex/execution/runner.py:62-73`). Terminality is judged by native strings
  (`TERMINAL_STATUSES`) decoupled from the status map, so transient states poll through correctly.
- Elapsed time accumulates from `interval_s`, not a wall clock, so an injected no-op sleep makes
  polling deterministic in tests.

A second adapter skeleton (`veridex/venues/sx_bet.py`) exists behind the same seam: its
`FakeVenueAdapter` (`sx_bet.py:85+`) is the deterministic offline venue (fixed 2.05 decimal price,
constructor-controlled fill/partial/reject/pending behavior) used by dry-run and the whole offline
suite; the real SX adapter remains a config-gated skeleton. The seam, not the venue, is the
product surface: more venues arrive behind the same fail-closed contract.

### 8.2 Polymarket — read and write

`PolymarketAdapter` (`veridex/venues/polymarket.py:331-615`) is bound at construction to a
resolved market + side + default size, with an injected book client (read) and optionally a write
client. Two distinct upstream clients serve it: market *resolution* uses the public Gamma API
(§8.3); the *order book and orders* use a **vendored, pinned, MIT-licensed Polymarket CLOB
client** kept in-repo under `veridex/venues/_vendor/polymarket_clob/` (import-path rewrites only,
no logic changes; provenance, file mapping, and license documented in
`veridex/venues/_vendor/README.md`). Vendoring means the exact reviewed bytes ship with the repo
rather than floating on a pip dependency.

**Read path** (`polymarket.py:392-468`): fetch the book for the side's token, parse levels
tolerantly (malformed/zero-size levels dropped, `polymarket.py:175-195`), sort the ask ladder via
the vendored order-book class (consumed only for its snapshot sort; its hazardous helpers with
latent empty-book `IndexError`s are deliberately avoided, `polymarket.py:446-468`), then walk the
ladder to a size-weighted average native fill price for `for_size` shares (`_fill_to_size`,
`polymarket.py:198-227`) and convert **once** to decimal odds. An empty/one-sided/unfillable book
degrades honestly — `price=0.0` (the no-price sentinel the edge law already treats as no-edge),
`size=0.0`, `native_price=None` — never a midpoint, never a fabricated price
(`polymarket.py:422-433`).

**Write path** (fail-closed by default): `get_order_status` requires
`settings.polymarket_write_enabled` (default false); a real `submit_order`/`cancel_order`
additionally requires `dry_run=False` (the safe default is `True`) **and** an injected write
client — the `_require_armed` triple gate (`polymarket.py:498-511`). When armed:

- Submit converts decimal odds → native share price → tick-rounded (`polymarket.py:530-533`), with
  a defense-in-depth guard: a native price outside `(0,1)` refuses **before the wire**
  (`polymarket.py:534-540`) — the venue would also reject it, but the money path never relies on
  the wire to catch a pathological price. Orders go out FAK.
- Status reads the **real matched fill** from the venue's order record: `filled_size` is
  `size_matched`, `native_price` the matched price (audit), `price` its decimal inverse — the
  request size is never echoed as a fill (`_order_status_from_raw`, `polymarket.py:297-323`).
  Reconciliation trusts the matched-size *number* over the status *label*
  (`_reconcile_status`, `polymarket.py:276-294`): a positive match is a fill regardless of label;
  an unmatched live/resting label stays non-terminal so polling times out to UNRESOLVED rather
  than guessing.

### 8.3 The resolver — real market structure, fail-closed selection

`resolve_market` (`veridex/venues/polymarket_resolver.py:211-292`) turns a structured reference —
`"1X2|home|full"`, `"1X2|away|full"`, `"1X2|draw|full"`, `"OU|<line>|full"` — into concrete
on-chain identifiers (`ResolvedMarket`: condition id, yes/no token ids, tick size,
`draw_market` flag; `polymarket_resolver.py:130-149`).

The structure it navigates was **verified live against the real Gamma API** (T13b,
`polymarket_resolver.py:8-30`): a World Cup fixture slug names an *event* containing many markets.
1X2 is **three separate binary Yes/No markets** ("Will Portugal win…", "…end in a draw?", "Will
Croatia win…"); the O/U totals ladder lives on the **sibling `-more-markets` event**, so an OU
lookup fetches that event and never falls back across events (`_lookup_slug`,
`polymarket_resolver.py:312-320`). Selection rules:

- `1X2|home` / `1X2|away` match the "Will \<TEAM\> win…" market **by normalized team name**
  (accent-stripped, punctuation-collapsed, alias-mapped — "USA" → "United States", "Bosnia &
  Herzegovina" ≡ "Bosnia and Herzegovina"; `_normalize_team` + `_TEAM_ALIASES`,
  `polymarket_resolver.py:86-127`), never positionally. Missing team identity fails closed.
- `1X2|draw` selects the "…end in a draw?" market and flags it `draw_market=True`; a live
  `side="draw"` maps to that market's **YES** token (DRAW = YES on the draw binary), and
  `side="draw"` on any *other* market raises rather than routing to a wrong YES
  (`side_to_token`, `polymarket_resolver.py:194-203`).
- `OU|<line>` matches the numeric line on **full-match** markets only — half/period totals and
  single-team totals are excluded by pattern (`_match_ou`, `polymarket_resolver.py:414-449`).
- **The cardinal rule**: zero matches, more than one match, an unknown ref type, an unsupported
  period, or any malformed field ⇒ `MarketUnavailable` (`polymarket_resolver.py:152-158,
  323-374`). The resolver **never guesses a token** — a wrong selection would route a real order
  to the wrong outcome.

### 8.4 The away bug — the best interview story in the codebase

**What was latent.** Before the fix, one shared label vocabulary served both "which outcome label
does this market carry?" and "which side does the caller want to bet?" — and `"away"` sat in the
NO-labels set (the intuition being home/away ↔ yes/no). But `1X2|away` resolves the *away-team-WIN*
market — "Will \<away team\> win…?" — where **YES = the away team wins**. Mapping the bet side
`"away"` to the NO token therefore routed an away bet to the **away-LOSES** token: a live order
that would have bought the exact opposite of the intended position, with real money.

**How it was caught.** By adversarial code review of the resolver against the live-verified market
structure — while the live path was still unwired, so it never touched money.

**Root cause.** One vocabulary serving two meanings: *outcome labels* (the literal strings a
market carries — always Yes/No or Over/Under in the target families; no target market uses
"Home"/"Away" as an outcome label) vs *bet sides* (what a caller asks to buy — where each 1X2 side
resolves to its own per-team market whose YES outcome is that team winning).

**The fix.** Decoupled label sets with the warning written into the source:
`_OUTCOME_YES_LABELS = {yes, over}` / `_OUTCOME_NO_LABELS = {no, under}` (used only by the market
parser) vs `_SIDE_YES_LABELS = {yes, over, home, away}` / `_SIDE_NO_LABELS = {no, under}` (used
only by `side_to_token`), under a comment block that names the bug: "a real-money side↔token bug
lived in the overlap … conflating them inverts a live away order"
(`veridex/venues/polymarket_resolver.py:64-81`). Plus end-to-end resolve→token regression tests in
`tests/test_polymarket_resolver.py`, so the swap can never silently return.

**Why it matters.** Every safety gate in §9 protects against *acting when you shouldn't*. This bug
was the other class — *acting correctly on the wrong object* — and it is why the resolver's
fail-closed posture (never guess, raise `MarketUnavailable`) is treated as a money gate, not a
data-quality nicety.

### 8.5 Write preflight — tri-state honesty, and `live_ready`

`run_preflight` (`veridex/venues/polymarket_preflight.py:175-370`) evaluates every real-order
precondition as a NAMED check with a **tri-state** verdict: `ok ∈ {True, False, None}`, where
`None` means **"operator must verify — this code cannot"** (`PreflightCheck`,
`polymarket_preflight.py:87-99`). Checks: `market_mapped`, `wallet_funded_usdc`,
`usdc_allowance`, `ctf_approval_exchange`, `ctf_approval_neg_risk`, `sig_type`,
`egress_reachable`, `kill_switch_ready`, `liquidity` (the only quote-dependent check — skipped
with zero quote I/O when any cheap check fails, `polymarket_preflight.py:330-344`),
`dry_run_state` (informational), `operator_fak_smoke`.

Two verdict aggregates with deliberately different semantics
(`polymarket_preflight.py:363-370`):

- `report.ok` = AND over every check *with a boolean verdict* — **`ok=None` items are excluded**,
  so `ok=True` can NEVER arm live (it does not mean the operator confirmed anything).
- `live_ready` = `ok` AND `ctf_approval_neg_risk is True` AND `operator_fak_smoke is True` — both
  operator-verify checks must be **explicitly** confirmed. Fail-closed default: `False`.

**The bug this tri-state fixed.** The neg-risk exchange is a separate contract needing its own
ERC-1155 approval, and the vendored balance-allowance surface **cannot distinguish it** — it has
no neg-risk selector. An earlier version returned a boolean "pass" for that check, which merely
mirrored the regular-exchange approval and **fabricated a pass** for an operator who had approved
the regular exchange but not the neg-risk one. The fix is the honest `ok=None` with the
explanation in the detail string, operator-suppliable as an explicit `True`/`False`
(`polymarket_preflight.py:278-299`). The 1-share FAK smoke is likewise recorded pending and
**never auto-run** (`polymarket_preflight.py:352-361`); it is a human decision by design
(`scripts/polymarket_smoke.py`, gated by `POLYMARKET_SMOKE=yes` + `POLYMARKET_WRITE_ENABLED` —
deliberate friction; `docs/operator-runbook.md`).

---

## 9. The live-money conjunction

### 9.1 The mode ladder

Source and execution are two orthogonal fields whose product is labeled by a **total** function —
an unmapped pair raises rather than guessing (`mode_ladder_label`,
`veridex/backtest/report.py:60-80`):

| `source_mode` × `execution_mode` | Label |
|---|---|
| replay × (none) | Replay |
| replay × paper | **Backtest** |
| live × paper | Live Paper |
| live × dry_run | Dry Run |
| live × live_guarded | Live Guarded |

A replay can never read as "Live"; a dry run can never read as an executed order. Defaults are
safe throughout: the Polymarket adapter constructs `dry_run=True`
(`veridex/venues/polymarket.py:366`), writes are disabled unless `POLYMARKET_WRITE_ENABLED` is
set, Studio deploys default `execution_mode="paper"` (`veridex/deploy/preflight.py:93`), and the
standalone runner's non-paper default is `dry_run` (`veridex_agent/run.py:342`). The safe state is
the state you get by doing nothing.

### 9.2 The full conjunction for one real order

A real Polymarket order requires **every** clause below; missing any one degrades the run to a
dry simulation:

1. **`execution_mode == LIVE_GUARDED` as a STRUCTURAL first conjunct.** The route selector arms
   only inside `if execution_mode == ExecutionMode.LIVE_GUARDED and _live_arm_gate(live_deps) is
   None:` (`veridex/competition/service.py:191-210`). Because the mode equality is the first
   conjunct, any other mode — including a defensively-passed `paper` or a **future fourth
   ExecutionMode value** — falls through to the Fake-adapter dry route *by construction*, not by
   enum-counting luck. Pinned by a parametrized test that feeds unexpected mode values with fully
   armed deps and asserts no arming (`tests/test_service_live_path.py:258`).
2. **Operator-supplied `LiveExecutionDeps`** (`service.py:97-126`) — the armed real-venue adapter
   bundle. Absent (the default `None`) ⇒ degrade, reason `missing_live_deps`.
3. **`live_ready is True`** — the preflight verdict that itself requires the two explicit operator
   confirmations (§8.5). Not `True` ⇒ degrade, reason `live_ready_false`.
4. **A genuine real-venue adapter** — the `PROVIDES_REAL_VENUE_QUOTE` marker check
   (`service.py:152-153`). A Fake ⇒ degrade, reason `non_real_adapter`.
5. **The adapter's own second lock.** Even a routed order must pass `_require_armed` inside the
   adapter — write-enabled AND `dry_run=False` AND an injected write client
   (`veridex/venues/polymarket.py:498-511`). **The money gate does not trust the routing layer**:
   two independent locks (route + adapter) must both open.
6. **Run-time guards**: an OPEN breaker denies pre-quote with **zero venue I/O**
   (`veridex/policy/gate.py:102-105`; end-to-end at `tests/test_service_live_path.py:338`); the
   live-only `max_stake_live_guarded` cap applies (`gate.py:110-113`;
   `tests/test_execution_safety_gates.py:231-258` proves it engages live and not on dry-run);
   quote-size coupling (§6.4); and the resolver's `MarketUnavailable` fail-closed rule (§8.3).

The three money gates beyond the mode conjunct live in ONE predicate — `_live_arm_gate`
(`service.py:135-154`) — read by *both* the arm decision and the degrade telemetry, so the arm
outcome and the recorded reason can never disagree (single authority again, at the routing layer).

### 9.3 No HTTP path passes live deps

`POST /competitions/{id}/start` calls `start_competition(...)` with **no `live_deps` argument**
(`veridex/api/router.py:885`) — the parameter exists only on the Python API
(`service.py:300-307`). Real money is therefore **operator-direct-only, by construction**: an
operator must build the armed adapter and the preflight verdict in code and hand them to
`start_competition` directly (`docs/operator-runbook.md`, step B6). No HTTP request, no deployed
agent, and no LLM proposal can arm the live path, because the arming inputs simply do not exist on
any wire surface.

### 9.4 Honest degrade — configured-live-but-unarmed runs say why

A configured `live_guarded` run that fails a money gate does not error and does not pretend: it
runs as a dry simulation AND emits an `EXECUTION_ROUTE` telemetry event —
`{requested_execution_mode: "live_guarded", effective_execution_mode: "dry_run",
degraded_because_not_armed: true, degrade_reason: <enum>}` — persisted before broadcast,
`evidence=False`, never sealed (`service.py:516-534`;
`build_execution_route_event`, `events.py:327-366`). The reason enum is exactly the
`_live_arm_gate` vocabulary: `missing_live_deps | live_ready_false | non_real_adapter`. An auditor
reads the degrade in the log instead of inferring it from config-vs-events; pinned end-to-end for
each reason by `tests/test_service_live_path.py:393-449`.

### 9.5 Status: no real order has ever been placed

The path is built, tested, and adversarially reviewed — and **no real-money order has been
placed**. The first 1-share FAK smoke is deliberately a human operator's decision, with its own
deliberate friction (env-gated script, wallet funding, on-chain approvals, then explicitly setting
`neg_risk_approved=True` / `fak_smoke_passed=True` into the preflight) — the full sequence is
`docs/operator-runbook.md` Part B. The runbook also defines the honest acceptance criterion: a
smoke that comes back `dry_run` means the path was *not armed* and counts as a **failed** smoke,
never a pass.

---

## 10. Strategies — Momentum v1 and Sharp Momentum v2

### 10.1 The strategy contract: propose, never grade

Every strategy is an `Agent` with an async `decide(market_state) -> AgentAction`
(`veridex/runtime/orchestrator.py:80-95`). Whatever it writes into `params` beyond
`market_key`/`side` — `reason`, `confidence`, `claimed_edge_bps` — is untrusted UX metadata the
law records and discards (§4.1). Strategies are stateful only over ticks already seen and fully
deterministic, so a `reproducible` proof mode is earnable (`veridex/strategies/momentum.py:13-17`).

### 10.2 Momentum v1 — the honest baseline

v1 (`MomentumStrategy`, `momentum.py:95-153`) flags the strongest-rising side whose net
`last − first` de-vigged probability move over a lookback window clears `min_momentum_bps`, with
deterministic tie-breaking (`select_momentum_action`, `momentum.py:49-92`). It is deliberately
naive — it false-positives on ordinary volatility that merely ends higher than it started
(`momentum.py:185-188`) — and it stays the golden-pinned baseline (§10.6).

### 10.3 Sharp Momentum v2 — the flagship pipeline

`SharpMomentumStrategy` (`momentum.py:251-433`) is a **false-positive-controlled line-movement
shock detector**, not an oracle. Per `(market_key, side)`, each tick flows through six stages
(`_score_side`, `momentum.py:325-372`; the pure math in `veridex/strategies/sharp_stats.py`):

1. **Logit-space level** — `logit(p) = ln(p/(1−p))`, symmetric-epsilon-clamped
   (`sharp_stats.py:41-54`; `_LOGIT_EPS = 1e-6` is deliberately sane-sized so a glitchy 0/1 feed
   tick clamps to a bounded value instead of a massive synthetic jump the detector would mistake
   for a sharp move, `sharp_stats.py:26-31`). *Why logit:* probabilities move additively in
   log-odds, linearizing a move's "distance" regardless of starting level — a 2%→3% move and a
   50%→51% move are not the same event, and logit space says so.
2. **EWMA smoothing** of the level (`s = α·x + (1−α)·s_prev`, applied incrementally,
   `momentum.py:343-345`) denoises single-tick spikes; the per-tick *movement* is the smoothed
   delta.
3. **Robust z-score** of the latest movement vs its own history: median/MAD with the 1.4826
   consistency constant, and the scored point excluded from its own reference
   (`robust_z`, `sharp_stats.py:83-111`). Median/MAD has a 50% breakdown point — a lone outlier in
   the window cannot corrupt the estimate the way a mean/std z would be corrupted. The **scale
   floor** matters for sports: a perfectly flat market has MAD = 0, and an unfloored z would
   return 0.0 — *missing* the most important case, a flat market that suddenly reprices. The floor
   turns that jump into a finite, large z.
4. **Directional Page-Hinkley** change-point confirmation on the smoothed level
   (`PageHinkley`, `sharp_stats.py:114-168`). *The bug story:* the original implementation
   returned a bare bool — "a change-point fired" — so a **downward** change-point could confirm an
   **upward** action. The fix makes direction part of the return type
   (`Literal["up","down"] | None`, `sharp_stats.py:38`), with the guard written into the class
   docstring ("a downward change-point can never be read as confirmation of an upward move") and a
   dedicated regression: a down-then-up bounce never fires the up side
   (`tests/test_momentum_v2.py:119`). The v2 gate requires `ph_dir == "up"` exactly
   (`momentum.py:368-369`).
5. **Persistence confirmation** — at least 2 of the last 3 smoothed movements share the shock's
   direction AND their cumulative logit move clears `persistence_logit`
   (`_persists_up`, `momentum.py:313-323`; the 2-of-3 constants are fixed invariants, not knobs,
   `momentum.py:244-248`). The gates compose with **AND, not OR**: a lone PH trip (single spike)
   or lone persistence run (slow drift) is not enough — the false-positive reduction *is* the
   point (`momentum.py:365-371`).
6. **Warmup, min-samples, and cooldown**: no action before `warmup_ticks` observed; no z before
   `min_movements` per-side samples; a fired market is suppressed for `cooldown_ticks`
   (`momentum.py:355-359, 409-413`). Strongest z wins per tick, deterministic tie-break
   (`momentum.py:417-418`).

**All ten behavioral parameters enter `config_hash`** — alpha, z_threshold, ph_delta, ph_lambda,
cooldown_ticks, warmup_ticks, min_movements, lookback, scale_floor, persistence_logit
(`sharp_momentum_agent`, `momentum.py:488-499`; pinned by
`tests/test_momentum_v2.py:254`). Same config ⇒ same sealed identity ⇒ reproducible backtests.

The market universe is gated to **clean families** — 1X2 and totals; player props are deferred
(noisier, less liquid price processes) — via `is_clean_family` and the `CLEAN_FAMILY_PREFIXES`
tuple, which now includes the real TxLINE totals key `OVERUNDER_PARTICIPANT_GOALS` after the
real-data run exposed its absence (`momentum.py:213-232`; §12.3).

### 10.4 Grounding and validation

The design is grounded in the line-movement literature — the claim class is "sharp, sustained
repricing carries information about the closing line" (Simon 2024, *Management Science*), not
"this model predicts soccer." Validation is by **operating-curve tapes**
(`tests/_sharp_momentum_tapes.py` + `tests/test_momentum_v2.py:81-132`): null noise stays under a
false-positive budget; an injected sharp move fires in the correct direction; slow drift is
quieter than v1; a single outlier never fires; sustained repricing fires after warmup; and v2
stays quiet on a tape where v1 demonstrably false-positives.

Two structural properties are proven, not asserted: **determinism** (same ticks ⇒ same actions,
`test_momentum_v2.py:186`) and **no lookahead** — for every k, the tick-k decision on the prefix
`[:k+1]` equals the tick-k decision on the full tape (`test_momentum_v2.py:190-196`). Prefix
invariance is the precise statement that no decision ever depended on a future tick.

### 10.5 Proposer-only, even for the flagship

v2's z-scores and `claimed_edge_bps` (`int(round(z*100))`, `momentum.py:427`) are **untrusted UX
metadata** like any agent's claims — surfaced in the Inspector's fenced block, never scored
(`test_momentum_v2.py:234-252`). The demo claim is calibrated to what the tests prove: "v2 reduces
naive-momentum false positives while still catching sustained repricing" — never "this proves
sharp money" (`momentum.py:208-211`).

### 10.6 Why v2 was wired additively

v1 remains the default and keeps its agent id (`momentum`); v2 is a separate agent id
(`momentum-sharp`) with distinct UI labels (`STRATEGY_LABELS`, `momentum.py:236-242`). The golden
fixtures pin sealed bytes that include v1's config-hash inputs — renaming or replacing v1 would
have invalidated the byte-for-byte baseline that guards the *seal path* during exactly the period
when the runtime was being rebuilt. Deliberate choice: **the byte-pinned goldens outrank naming
purity.** The flagship demo runs v2 explicitly (`scripts/demo_phase2d.py:45, 58`).

---

## 11. The deploy platform

### 11.1 Typed + bounded configs — never a weird-but-hashable instance

A Studio deploy submits a `DeployConfig` (`veridex/deploy/preflight.py:62-148`): template id,
agent id, strategy family, source/execution mode, market/venue allowlists, policy knobs, window
config, and all ten v2 detector knobs. Types are enforced at the pydantic wire boundary (a
non-numeric knob is a 422 before it can be hashed); numeric **bounds** are enforced by the named
`config` preflight check over an explicit bound table (`_NUMERIC_BOUNDS`,
`preflight.py:37-54`) — e.g. `alpha ∈ (0,1]`, `min_movements ≥ 2`.

The subtle case is the **cross-field validator**: `lookback ≥ min_movements` for
`momentum-sharp` (`preflight.py:197-198`, mirrored in the runner config's model validator,
`veridex_agent/config.py:89-101`). Both fields can be individually valid while their combination
is *inert* — a lookback window that can never retain enough samples for robust-z to fire produces
an agent that is deployable, hashable, and permanently silent. Rejecting valid-but-inert configs
at the gate is part of "never a weird-but-hashable instance."

### 11.2 The named, fail-closed deploy preflight

`run_deploy_preflight` (`preflight.py:279-309`) is pure and offline over already-fetched inputs,
returning four named checks: `config` (bounds + cross-field), `feed_health` (mode-aware: a replay
deploy verifies its replay *source* resolved to non-empty ticks; a live deploy fail-closes on a
disconnected or stale feed, `preflight.py:205-233`), `market_mapped` (required only for
`live_guarded`; `ok=None` not-applicable otherwise, `preflight.py:236-253`), and `policy_limits`
(sane caps; a non-paper deploy with an empty market or venue allowlist is rejected as
nothing-to-trade, `preflight.py:256-276`).

The route (`veridex/api/deploy.py:269-310`) turns any `ok is False` into a **422 naming every
failing check**, with the full check list in the response body. On failure: **no instance row is
persisted and no run starts** — a refused deploy leaves zero state. `config_hash` is pinned only
after validation passes (`deploy.py:312-321`).

### 11.3 Async deploy: return-before-seal, tracked, never silently lost

On a passing preflight the route mints `run_id` up front, persists the instance, then launches the
run as a tracked `asyncio.Task` and **returns immediately** — the response is exactly
`{instance_id, config_hash, policy_hash, run_id}` (`DeployResponse`, `deploy.py:57-70,
342-373`); no handles, secrets, or traces cross the wire. The task set lives on `app.state` and is
cancelled on shutdown via the lifespan hook (`cancel_deploy_tasks`, `deploy.py:376-389`). A
done-callback logs any background failure with the run id and full `exc_info` — a failed deploy
run is never lost to asyncio's GC warning, and an operator can explain why a subsequent
`/runs/{id}/verify` 404s (`deploy.py:347-366`).

### 11.4 The durable AgentInstance

The **store is the source of truth; `app.state` holds only task handles** (`deploy.py:183-186`).
Ordering is persist-then-launch (`deploy.py:336-345`): the durable row exists before the run
starts, so a deployed instance is never app-state-only and survives a process restart — loadable
from a *fresh* store after `app.state` is cleared (exercised by `tests/test_deploy_endpoint.py`;
the Postgres round-trip is the one operator rehearsal step, `docs/operator-runbook.md` Part A).

The record (`veridex/deploy/instance.py:78-125`) pins: submitted + effective configs,
`config_hash`, `policy_hash`, modes, allowlists, `run_id` — and the **persisted preflight-check
audit**: the named verdicts that gated this launch ride on the instance, so "why did this agent
launch?" is answerable from the durable record alone. Lifecycle: `PENDING → RUNNING →
SEALED | FAILED` (`DeployStatus`, `instance.py:25-42`), with the status vocabulary single-sourced
into the store's CHECK constraint (`deploy_status_values`, `instance.py:66-75`) so the enum and
the database can never drift. A shutdown-cancelled run is honestly left `RUNNING` — it was
running when the process died (`deploy.py:252-254`).

**`last_failure_reason` is a controlled enum** — `preflight_failed | seal_failed | lane_failed |
runtime_error` (`DeployFailureReason`, `instance.py:45-63`). Raw tracebacks go to the server log
only; a framework trace shape can never leak through the durable record or a response
(`deploy.py:212-265`).

### 11.5 The single runner seam

`veridex_agent/run.py::standalone_run` (`run.py:333-458`) is THE one runner: it dispatches on
`window` to compose either the replay seal (`_seal_replay`, byte-identical to the pre-deploy
standalone core, `run.py:127-190`) or the live seal (`_seal_live` — which *calls*
`run_live_window` rather than re-implementing the live composition, `run.py:193-232`), then the
shared tail: self-verify, the optional downstream execution lane, and the non-scoring
agent-instance pin (`config_hash` + `policy_hash` + window + modes, `run.py:435-442`).

The deploy endpoint calls it (`veridex/api/deploy.py:225-251`), the CLI calls it
(`veridex_agent/cli.py:42-101` — `veridex-agent run --config agent.toml` prints
`[VERIFIED] run_id=… manifest_hash=…`), and the SDK/Docker path is the same CLI
(`docs/deploy-your-own-agent.md`). Agent construction likewise flows through the single
`build_agent` dispatch (`veridex_agent/config.py:132-164`; the route maps its wire config onto
`AgentRunConfig` in exactly one place, `deploy.py:111-151`). **There is no parallel deploy path**
— which is also why an earlier defect ("the deploy loop only worked with test-injected
dependencies") could be fixed once, at the seam: the default app resolves a real replay source
(the deterministic in-code demo fixture) when no deps are injected (`deploy.py:188-199`), so the
headline Studio flow works in the real app with zero credentials.

### 11.6 One flow to proof

The deployed run persists to the shared store *under the pre-known `run_id`* at seal time
(`standalone_run(store=..., run_id=...)`, threaded from `deploy.py:225-251`), so a deployed
agent's run verifies through **the same `POST /runs/{id}/verify`** as an arena run or a demo run
(§5.4). There is no separate "deployed verification" — one endpoint, one law, one proof shape.

---

## 12. Backtesting and the real-data experiment

### 12.1 The backtest engine is the runtime

There is no external backtest library and no second engine: a backtest is a ReplayPack replayed
through the **same** `CompetitionRun` the live loop drives (`veridex/backtest/runner.py:1-10`).
`run_backtest` (`runner.py:38-96`) loads pack ticks through the one normalizer with hash
verification on (a tampered pack refuses to replay), pins a **deterministic run id from the pack's
`content_hash` + window id** (`bt_{hash[:12]}_{window_id}`, `runner.py:69-71` — same pack + same
window ⇒ the same sealed run, byte-identical across repeats), feeds every decision tick, feeds the
final tick as the *closing* tick for a `pre_match` window (agents never decide on the close), and
finalizes.

The `BacktestReport` (`veridex/backtest/report.py:132-189`) is a **pure projection of the sealed
run** — it re-reads no venue, no feed, no LLM, so it can never smuggle a fresh trust claim past
the seal. It carries tamper-evident lineage (pack id + `content_hash` + `run_id` +
`evidence_hash`), the honest mode label (§9.1), the scored-CLV distribution (values listed, never
just a mean), a min-edge threshold-sensitivity sweep, `law_valid_rate` vs the honestly-null
`policy_pass_fail_rate` (§4.5), explicit assumptions (slippage 0, costs 0 on the paper path), and
`real_executable_edge_bps = None` always — an explicit null on the fake/paper venue, never a
fabricated number (`report.py:17-20, 169-170`).

### 12.2 The real-data experiment — design

After the synthetic pipeline was proven, the flagship was pointed at reality exactly once
(task T22-real; the public account is `README.md:68-81` and `docs/submission.md:47-57`):

- **Data**: the full real TxLINE odds history for a finished World Cup fixture — **USA v Bosnia,
  65,156 StablePrice updates spanning ~6 days** — pulled with one read-only
  `GET /api/odds/updates/{fixtureId}` call (the endpoint depth documented in
  `docs/txline-feedback.md:11-14, 21-32`).
- **Packaging**: a content-hashed ReplayPack via the same `pack_from_session` transform (§2.5).
- **Protocol**: run Sharp Momentum v2 through the sealed pipeline **once — no tuning, no fixture
  shopping**. The raw vendor odds stay local (licensed data, not redistributed;
  `docs/submission.md:72`); what ships is the pipeline, the fixes, and the sealed-proof
  discipline.

### 12.3 What happened — in three honest parts

**(a) 1X2 — zero fires, correctly.** The USA line moved **+907 bps**, but *smoothly*, across
~1,876 updates. Max robust-z reached **1.13**, below the 2.5 shock gate → the detector never
fired. That is correct behavior for a *shock* detector — smooth multi-day drift is exactly what
the persistence/PH/z stack is built to ignore. It is *also* an honest coverage-gap signal: a shock
detector alone under-covers smooth pre-match repricing. Both readings are true, and the platform
reports both.

**(b) Totals — the run exposed a real bug, then fired on real data.** The clean-family gate
contained `"OU"`/`"TOTAL"`-style prefixes — but real TxLINE totals arrive as
`OVERUNDER_PARTICIPANT_GOALS` (§2.3). The synthetic demo tape, which used the literal `"OU"` key,
had **masked** the gap: real totals were silently ineligible. The fix added the real family to
`CLEAN_FAMILY_PREFIXES` with the story in the comment (`veridex/strategies/momentum.py:213-219`)
and a regression pinning the real key (`tests/test_momentum_v2.py:211`). After the fix, v2 fired
**twice on real data** (z = 3.43 and 2.51) — but on **O/U 0.5**, a near-certain "will there be ≥1
goal" line. A genuine detection, and a market-selection lesson the strategy owns: degenerate
near-certain lines can dominate a shock detector's attention.

**(c) Scoring — the law refused to fabricate a close.** The O/U 0.5 line's closing state was
**suspended** — near-certain markets suspend pricing before the close, so no priced closing value
exists (behavior documented, with the ask that TxLINE document it too, in
`docs/txline-feedback.md:55-64`). `recompute`'s closing validation returned `closing_suspended`
(`veridex/law/recompute.py:86-99, 147-149`), scoring both fires **invalid** rather than imputing a
last-known price. Net result: **avg CLV = None, 0 scored picks, confidence honestly "low"** — and
the sealed run still verifies end-to-end.

The zero-scored-pick shape also exposed the **confidence-counting bug** — the tier keyed off
law-valid decisions (which include WAITs), so an abstention-heavy run read "high" — fixed on both
the leaderboard and report surfaces the same day (§4.5).

### 12.4 Why the null result is the product

It would have been trivial to impute a close, count WAITs as confidence sample, or lead with the
synthetic demo's rosy number. Instead the run surfaced two real bugs, both were fixed, the
pipeline was re-run once, and the result was reported as the data said it. **You cannot fake a win
on Veridex — and Veridex didn't fake its own.** A platform whose whole thesis is "agents can't
grade themselves" must be willing to publish its own agent's honest zero.

### 12.5 The predeclared strategy roadmap

The roadmap falls out of the run and is **predeclared, not retrofitted** (tracked as explicit
follow-on tasks; also in `README.md:232-238`):

1. A **cumulative-drift template** (`CumulativeDriftAgent`) for smooth multi-day repricing — the
   coverage gap (a) exposed.
2. A **market-quality eligibility filter** excluding degenerate near-certain lines — the
   market-selection lesson (b) exposed.
3. **Predeclared in-play evaluation windows** — so in-running evaluation is committed before the
   data is seen, the same discipline as everything else.

Each will be evaluated the same honest way: sealed runs, recomputed CLV, published nulls if nulls
are what happens.

### 12.6 Run-001 & Run-002 — the roadmap executed (18-fixture strategy results)

The §12.5 roadmap was then run. Both results are recorded cold in the research record
(`.omc/research/run-001-run-note.md`, `run-002-run-note.md`); the honest framing below is the same
one the docs and UI carry.

**Run-001 — candidate rung-1 CLV signal.** Across **18 finished World Cup fixtures** (market-quality-filtered
eligible universe, pre-kickoff decisions, CLV vs the CON-040 kickoff close), the `CumulativeDriftAgent`
averaged **+61.19 bps CLV** and **beat all three acting deterministic baselines** on this sample
(favorite +4.61, threshold-move −126.5, seeded-random −341.67; concentration
`top_match_share_of_net_pct = 27.2%`, 10/18 fixtures net-positive). This is a **candidate CLV signal,
not proven executable alpha**: rung-1 CLV against the sharp TxLINE close (no venue leg, no fills),
effective *n* ≈ 18 fixtures (the ~19.7k drift picks are autocorrelated intra-match), so it is
*directional evidence, not a statistical proof.* The runner self-verifies against a pre-run stamp and
reproduced the committed eligible-universe hash exactly (`universe_verified: True`).

**Run-002 — rung-2 estimated venue result (the trust moat).** Priced against a time-aligned Polymarket
1X2 mid under a 15-minute bounded-staleness bound (self-verified `COVERAGE_ARTIFACT_HASH` +
`VENUE_SOURCE_ID`), the lane matched **94.7% of drift's in-scope 1X2 decisions** (37,218 / 39,290;
denominator scoped by market family, so legit-unmatched 1X2 decisions still count *against* coverage).
The estimated edge was a near-perfect **monotonic longshot ramp** — **+607 bps** (0–20% prob) → +304 →
+90 → +63 → **+33 bps** (80–100% prob) — the fingerprint of a favorite-longshot / de-margin-scale
divergence, **not** strategy-specific dislocation. Across the haircut ladder the mid/favorite bands go
negative (−60/−87/−117 bps), so the "survives 300 bps" headline is longshot-driven only. Conclusion:
**Run-002 did NOT demonstrate executable venue edge.** These are estimated **mids, not fills**;
`real_executable_edge_bps = None`; no profit and no fillability claim. Two real-data integration bugs
(a ts-units misdiagnosis and a market-identity keying gap) were found and fixed between the milestone
gate and this run — the synthetic C-lane tests passed because they fabricated matching keys on both
ends of the seam; only the real run exercised the correspondence.

**C/P2 — Polymarket longshot-divergence falsification (future work, not implemented):** all-outcome
Polymarket normalization (the decisive test — if the ramp collapses, much of it was a scale artifact);
measure divergence over *all* matched decisions (not just drift's fired picks); a later-price
convergence test; bid/ask/depth instead of mids; a larger fixture sample. The instrument did its job —
it built the venue lane, priced the decisions, and made it impossible to mistake a structural artifact
for alpha; the rung-2 verdict is a sober *not-demonstrated*, with a well-posed falsifiable question
left on the ledger.

---

## 13. The frontend

### 13.1 Contracts-first wire types and adapters

The API client (`apps/web/lib/api.ts:1-16`) binds a **frozen wire contract** (`lib/wire.ts`,
mirrored from the backend contract file) and maps it to screen view-models. The header states the
boundary: **the frontend never reimplements law/scoring/checks** — verify is an authoritative
backend recompute the client only relays. Trust-critical mappings are exhaustively unit-tested
(all 7 lowercase checks preserved with real statuses and no CLV in checks; the WD-7 confidence
fields preserved on leaderboard rows; wire `verified` + checks preserved onto the verify
view-model), and **representational gaps are declared in a comment** rather than silently filled —
fields the frozen contract doesn't carry stay defaulted or come from their real sources.

The WebSocket client (`apps/web/lib/ws.ts:1-20`) consumes the canonical event log only (ops
telemetry is a separate channel and never enters the projection), normalizes the real
`CompetitionEvent` wire frame, and **never silently drops**: a sequence gap or slow-client buffer
overflow emits a gap notice and disconnects so the cockpit resyncs from the persisted log.

### 13.2 The honest-state doctrine

- **Unsupported fields don't render.** Absent optional telemetry renders "—", not a fabricated
  value (`veridex/runtime/runtime_events.py:35`).
- **Replay is never dressed as live** — mode labels come from the total ladder (§9.1) and the
  glossary distinguishes source vs execution mode (`apps/web/lib/glossary.ts:33-40`).
- **The "Config pinned ✓" affordance deliberately computes NO client-side hash.** Real hashes come
  only from the backend at deploy/seal time; a client-computed digest would be a fake proof
  affordance. The Studio and competition-create screens render the words "Config pinned ✓" with a
  frozen-at-create caption and the tests assert **no 0x-prefixed digest may ever appear** there
  (`apps/web/components/screens/StudioScreen.test.tsx:136-152`,
  `CreateCompetitionScreen.test.tsx:107-113`; glossary entry `config_pinned`,
  `glossary.ts:49-52`: "post-run hashes live on the Proof Card").

### 13.3 The untrusted-LLM fence in the Inspector

The Decision Inspector renders the agent's proposal beside the law's recompute, with the agent's
`reason`/`confidence`/`claimed_edge_bps` extracted into an explicitly-named
`untrusted_llm_metadata` block by the backend (`veridex/api/router.py:600-612`) and fenced in the
UI as `NOT AN INPUT TO SCORE`. The two columns — what the model said vs what the law computed —
are the product's thesis rendered as a screen.

### 13.4 The edge display gate and glossary discipline

§7.3 covers the gate (`lib/edge-gate.ts`). The glossary (`lib/glossary.ts:1-3`) is single-source
by decree: "This text IS the doctrine — screens MUST pull from here, never inline their own
microcopy. Do not paraphrase." Every term of art a screen shows (fair value, mispricing gap,
executable edge, CLV, window CLV, pending, checks-vs-metrics, proof mode, Kelly, seq, anchor)
resolves to one pinned definition.

### 13.5 Token-disciplined design system

The design system is single-source tokens with build-enforced conformance — no raw hex in
components (token-conformance tests live in `apps/web/__tests__/token-conformance.test.ts`; the
README's quality section records ~420 frontend tests, `README.md:306-310`). This is a smaller
sibling of the same idea as everywhere else: one source of truth, drift caught by machine.

---

## 14. The trust-boundary registry

The SEC-style invariants, each with where it is enforced. This table is the system's constitution
— configs, agents, and even most refactors cannot move anything in it.

| # | Invariant | Meaning | Enforced at |
|---|---|---|---|
| 1 | **Checks ≠ metrics** | CLV is a metric, never a CheckId; checks certify the record, metrics rank performance | Frozen 7-member enum (`veridex/checks/result.py:22-31`); metrics in a separate block (`checks/build.py:518-558`); UI mapping tests keep CLV out of checks (`apps/web/lib/api.ts` tests) |
| 2 | **No hardcoded PASS** | Every check recomputes from sealed data and can fail; fail-closed on exception | Each builder in `checks/build.py` (§5.2); tamper tests in `tests/test_checks_integrity.py` |
| 3 | **Telemetry never sealed** | Runtime/ops events can never enter evidence, scores, or ranks | Structural: RuntimeEvent lacks `sequence_no`/`evidence`/`payload_hash` (`runtime/runtime_events.py:1-13`); feed health doctrine (`ingest/feed_health.py:8-11`) |
| 4 | **Receipts non-scoring** | A fill can never become proof or skill evidence | `evidence=False` constructors (`competition/events.py:369-448`); `receipt_separation` check (`checks/build.py:448-475`); causal-inertness tests (§5.5) |
| 5 | **CLV-only ranking** | Confidence, Kelly, eligibility, anchor status never enter a rank key | `_rank_key` in both `scoring.py:217-238` and `leaderboard.py:203-229` |
| 6 | **Honest states** | No fabricated data anywhere: no fabricated fills (UNRESOLVED), closes (degrade), prices (no-price sentinel), labels (total mode ladder), or provenance (unknown-provenance fail-safe) | `venues/base.py:187-225`; `runtime/live_runner.py:363-407`; `venues/polymarket.py:422-433`; `backtest/report.py:60-80`; `scripts/demo_phase2d.py:152-181` |
| 7 | **Pre-run config pinning** | `config_hash`/`policy_hash` are pinned before launch, only after validation | `api/deploy.py:312-339`; `veridex_agent/config.py:103-115`; `competition/service.py:268-297` |
| 8 | **Runtime-neutral proof contracts** | Any runtime feeds the same evidence/telemetry contracts via adapters; the proof layer doesn't know which runtime ran | `runtime/runtime_protocol.py`; `runtime_events.py:10-13` |
| 9 | **No secrets in repo/image** | Credentials live in typed config/env, resolved inside seams; receipts and configs are secret-free by contract | `veridex_agent/config.py:1-7`; `execution/models.py:76-95`; `ingest/txline_auth.py:1-9` |
| 10 | **Confidence keys off scored picks** | Abstentions can never dress up as a high-confidence record | `leaderboard.py:196-199`; `backtest/report.py:299-304` |
| 11 | **LLM boundary** | Zero LLM SDK imports in law/scoring/leaderboard/verifier/checks/ingest/policy; the explainer may import nothing *from* the trust path | Static AST audit (`verifier/import_audit.py`), run as a live check (`checks/build.py:92-113`) and as tests (`tests/test_explainer_boundary.py`) |
| 12 | **Single execution authority** | Only the policy gate decides; the runner never grows a shadow gate | Source-grep test (`tests/test_policy_gate.py:155-165`); single-authority `_live_arm_gate` at the route layer (`competition/service.py:135-154`) |
| 13 | **Sealed-prefix discipline** | Evidence hash covers exactly the sealed prefix; live stream is a verified projection of it | `runtime/evidence.py:19-36`; prefix-parity assertion (`competition/service.py:564-592`); store CHECK constraints on instance status (`deploy/instance.py:66-75`) |

---

## 15. Testing philosophy

### 15.1 RED→GREEN TDD as the default motion

Trust-bearing modules were built test-first: the failing test exists before the behavior (visible
in module docstrings that name their driving tests, e.g. `runtime/evidence.py:3-4`,
`law/recompute.py:1`). The suite is fully offline — every network seam is injectable (streams,
close fetches, book clients, RPC clients, anchor functions), so 1,007 tests run with zero
credentials and zero I/O.

### 15.2 The golden byte-for-byte suite

§5.6 in full. One sentence for the interview: *every refactor since the baseline has left the
sealed bytes provably identical, because a four-line test compares the current runtime's output
byte-for-byte against committed fixtures* (`tests/test_orchestrator_golden.py:17-19`).

### 15.3 Revert-proofing: break it on purpose

A trust test is only trustworthy if it fails when the guarantee breaks. The suite includes
deliberate tampering — doctored `clv_bps` rows (§5.2), the pending_horizon relabel evasion
(`tests/test_checks_integrity.py:430-503`), a falsifiable manifest binding
(`tests/test_standalone_run.py:161`) — and negative-space tests: a Fake adapter with a plausible
venue price still reads `real_venue_quote=false` (`tests/test_execution_safety_gates.py:268`);
unexpected execution modes with fully-armed deps still never arm
(`tests/test_service_live_path.py:258`).

### 15.4 Structural tests: audits over intentions

- **Trust-path import audit** — AST-walks the seven trust targets for LLM SDK imports, asserting
  each target *exists* first so a deleted directory fails closed instead of passing vacuously
  (`checks/build.py:92-113`).
- **Single-authority source grep** — the runner's source may not contain `.allows(` or
  `circuit_open` (`tests/test_policy_gate.py:155-165`).
- **Causal inertness** — dry-run vs paper over identical inputs: byte-identical scores and
  evidence blocks (`tests/test_standalone_run.py:220-256`); proof-card skill block identical with
  and without fills (`tests/test_execution_integration.py:165`).
- **No-lookahead prefix invariance** — every prefix decision equals the full-tape decision
  (`tests/test_momentum_v2.py:190-196`).
- **Persist-then-broadcast ordering** — asserted per event
  (`tests/test_evidence_broadcast.py:134`).

### 15.5 The review process — and the real bugs it caught

Every task went through a two-stage review (spec-compliance, then code-quality), with independent
adversarial cross-model review at milestone gates. That process — not luck — caught, before they
could matter:

1. **The fabricated neg-risk preflight pass** — a boolean the client couldn't actually back;
   fixed to tri-state `ok=None` operator-verify (§8.5).
2. **The away side↔token swap** — an away bet mapped to the away-loses token; caught while the
   live path was unwired (§8.4).
3. **The confidence overclaim** — WAIT abstentions counted as CLV sample (§4.5).
4. **A deploy loop that only worked with test-injected dependencies** — the default app had no
   real replay source; fixed at the single seam so the honest Studio flow works with zero
   credentials (§11.5).
5. **A false-green environment break** — a run that reads green because its environment silently
   changed under it is the same overclaim class; the golden generator's own determinism self-check
   (`tests/golden/generate_golden.py:127-135`) and env-independent fixtures
   (`generate_golden.py:31-34`) exist for exactly this reason.

Each fix carries a regression test, and most carry the story in a source comment where the next
maintainer will trip over it.

---

## 16. Design-decision ledger

Every consequential decision, the alternative rejected, and why. This is the interview cheat
sheet.

| Decision | Alternative rejected | Why |
|---|---|---|
| One run / N agents on identical inputs (`orchestrator.py:10-13`) | One run per agent | Head-to-head CLV comparability: same ticks, same per-market closing snapshot for everyone; rank differences are strategy, not feed luck. Also fixes a prior model where un-timeouted agent calls could hang the loop |
| Sealed-prefix hashing with a derived tail (§3.3) | Hash the whole event log including scores/receipts | Scores must be *recomputable* and receipts *non-scoring*; hashing them would freeze derived data into evidence and make "recompute fresh" meaningless. Instead: prefix sealed, tail derived, and `metrics_recomputed` catches doctored tail rows |
| `run_id` excluded from the evidence hash (§5.1) | Bind run identity into the hash | Identical inputs ⇒ identical evidence bytes — third parties can re-run and compare; provable via the dry-vs-paper byte-equality test. Run identity binds at the manifest layer instead |
| Two-phase policy gate (§6.2) | Single-pass evaluation | Don't pay venue latency/exposure for an order deterministic policy would kill; and the single-pass design had a real inert-slippage bug (`gate.py:5-8`) |
| Pure frozen circuit breaker with injected clock (§6.3) | A stateful breaker service reading `time.time()` | Same events ⇒ same state: replayable, deterministic tests, no clocks/sleeps; the mutable cell is one small, visible carrier |
| Single execution authority + source-grep enforcement (§6.3) | Runner-side guardrails alongside the gate | Two gates drift; the deny reason must be minted in exactly one place, and a test greps the runner to keep it that way |
| Decimal-odds doctrine, structural (`Quote.price` + `native_price`) (§7.1) | Ambiguous `.price` in venue-native units | One trust-core unit; a native `q` leaking into `.price` corrupts every downstream number; the field split makes the bug unrepresentable |
| `real_venue_quote` as an explicit class marker (§7.2) | Infer from a number's presence | A Fake adapter produces plausible numbers; honesty must be declared and fail-closed, not statistically inferred |
| Additive v2 beside v1 (§10.6) | Rename/replace v1 and regenerate goldens | The byte-pinned goldens were guarding the seal path during the runtime rebuild; the safety net outranks naming purity |
| `ok=None` tri-state preflight (§8.5) | Boolean checks | A boolean for an unverifiable fact is a fabricated pass; `None` = "operator must verify" and can never arm live |
| Structural mode conjunct first (`service.py:191-210`) | Contextual mode checks scattered at callers | A future fourth mode degrades to dry *by construction*, not by enum-counting luck; pinned by the unexpected-mode test |
| Degrade-to-dry with a recorded reason (§9.4) | Hard-refuse (raise) on an unarmed live run | A refusal loses the run and hides the cause; an honest degrade preserves the sealed proof, keeps operations flowing, and self-describes via `EXECUTION_ROUTE` + a reason enum. Chosen deliberately: honest degrade over hard refusal |
| Operator-direct-only live arming (§9.3) | An HTTP arming parameter with auth | Auth can be stolen or misconfigured; a wire surface that *doesn't exist* cannot be exploited. Real money enters only through code an operator writes |
| Persist-then-launch deploys (§11.4) | Launch-then-persist | A crash between the two must never leave a running run with no durable record; a refused deploy leaves zero rows |
| Controlled failure-reason enum (§11.4) | Persist raw tracebacks | Trace shapes leak stack internals through durable records and responses; the enum bounds the surface, logs keep the detail |
| Label-set decoupling in the resolver (§8.4) | One shared side/outcome vocabulary | The overlap held a real money-inverting bug; two meanings get two vocabularies |
| Provenance travels with numbers (§7.5) | A provenance page/footnote | A metric quoted apart from its provenance is an overclaim waiting to happen; the caveat rides inline on every surface, and unmarked never reads as real |
| Law refuses suspended/missing closes (§4.1, §12.3c) | Impute last-known price | An imputed close is a fabricated score; `closing_suspended`/`closing_missing` are honest reasons and pending is a sentinel, not a zero |
| Window config sealed into evidence (§3.2) | Trust row labels | Score rows aren't hashed; sealing `{end_rule, horizon, end_ts}` lets the verifier re-derive `pending_horizon` and closes the relabel evasion |
| Persist-before-broadcast (§3.5) | Broadcast-then-persist (lower latency) | A spectator must never see an event that isn't durable; a crash can't strand claims that were shown but never sealed |
| One runner seam for deploy/CLI/SDK (§11.5) | A separate deploy runner | Parallel paths drift; one seam meant the injected-deps-only bug was fixable once, everywhere |
| Vendored, pinned venue client (§8.2) | A pip dependency | The exact reviewed bytes ship in-repo (MIT, provenance documented); no supply-chain float under the money path |
| FAK/FOK only; GTC unrepresentable (`base.py:96`) | Support resting orders | A resting order is unbounded exposure the lane's receipt model can't honestly track; the type system forbids it |
| Confidence tiers off scored picks (§4.5) | Off law-valid decisions | A thousand WAITs must never read "high confidence"; the real-data run proved the failure mode exists |
| Backtest run id from `content_hash` (§12.1) | Fresh UUID per backtest | Same pack + window ⇒ same sealed run: reproducibility is a property, not a promise |

---

## 17. Hard-questions appendix (interview prep)

Likely hostile questions, with the honest answers and the code behind them.

**Q1. Your flagship made zero picks on real data — why should we care?**
Because that null is the product working. Veridex's claim is not "our strategy has edge" — it is
"you can *verify* what an agent did." On real data the platform (a) correctly declined to fire a
shock detector on smooth drift (max z 1.13 vs a 2.5 gate), (b) exposed and fixed two real bugs (a
market-family gap and a confidence overclaim), and (c) refused to fabricate a closing price for a
suspended line, scoring honestly to zero — and the sealed run still verifies. A platform that can
publish its own agent's zero is exactly the platform whose green numbers you can believe. §12.

**Q2. Couldn't the agent just lie about its edge?**
It can — and it changes nothing. `claimed_edge_bps` is read out of the action params only to be
recorded as untrusted metadata (`law/recompute.py:155-157`); the scored CLV comes from the law's
recompute over sealed market snapshots. The UI fences the claim as `NOT AN INPUT TO SCORE`
(§13.3). Two code paths, no flow between them — and the trust path is statically audited to
contain zero LLM SDK imports, fail-closed if a trust directory goes missing (§5.2).

**Q3. What stops YOU from tampering with the leaderboard?**
The verify endpoint recomputes everything fresh from sealed bytes — it never echoes stored scores
(`verifier/recompute.py:171-233`). Tamper a sealed event → `evidence_integrity` fails. Tamper a
score row (not hashed) → `metrics_recomputed` re-runs the law from the sealed snapshots and
catches the divergence — including the coordinated-tamper and relabel evasions (§5.2). The
manifest hash is anchored on Solana, so even the operator can't rewrite history the anchor
already committed to. We tampered on purpose in the suite to prove each of these fails.

**Q4. Why is the anchor only a Memo on devnet?**
Scope honesty. The anchor's job is a public, timestamped commitment to the manifest hash — a Memo
does exactly that, and the payload is verbatim the 64-hex manifest hash (`chain/anchor.py:45-50`).
Devnet is where a hackathon's real-money-adjacent system should live; the anchor status is honest
about it (`not_applicable`/`pending` offline, severity `info`), and mainnet anchoring is listed as
next scope, not claimed. What matters is the binding chain: sealed prefix → evidence hash →
manifest (+ per-domain Merkle root-forest) → anchored hash — every link recomputable (§5.3).

**Q5. What's actually live vs simulated?**
Precisely labeled, by a total function that raises on unmapped pairs (§9.1). Really live today:
TxLINE auth (including the real on-chain devnet subscribe tx), the SSE odds stream, the full
odds-history fetch, the Polymarket Gamma resolution and CLOB order-book reads, and Solana devnet
anchoring. Simulated/offline: the demo tape (self-labeled synthetic on every surface), dry-run
receipts (labeled `dry_run`), the Fake venue. Built-but-never-exercised-with-money: the Polymarket
write path (§9.5). Designed-not-wired: custody/payouts (the UI says so).

**Q6. Has real money been traded?**
No — and that is a decision, not a gap. The write path is built, reviewed, and double-locked; the
first 1-share FAK smoke is deliberately a human operator's step with deliberate friction
(env-gated script, on-chain approvals, explicit operator confirmations feeding `live_ready`).
Nothing in the repo places a real order on its own, and no HTTP path can arm one (§9.3, §9.5,
`docs/operator-runbook.md`).

**Q7. Why decimal odds as the internal unit?**
The trust core computes EV as `p·price − 1`, which requires decimal odds; policy and the UI read
the same unit. Venues price natively (Polymarket in share prices `q`), so the adapter converts
exactly once at the boundary and keeps the native value as an audit field — structurally, in the
type (`Quote.price` vs `Quote.native_price`). A unit mix-up at the money path is the kind of bug
you make unrepresentable, not unlikely (§7.1).

**Q8. How is this production-ready if custody isn't wired?**
"Production-ready" is claimed per-layer, honestly. The proof/verification layer is complete and
falsifiable today. The execution safety layer is complete through the operator gate. Custody
(Prize Vault) is designed and visible but not wired — and the UI says so rather than faking a
payout. The production posture is the discipline itself: fail-closed defaults, honest degrades,
controlled failure vocabularies, durable persist-then-launch records (§11, §14).

**Q9. Why should two agents' CLV be comparable at all?**
Because the runtime makes their inputs identical by construction: one run, all agents deciding on
the same frozen snapshot per tick, scored against the same per-market closing snapshot computed
once and shared (§3.1). And CLV is measured inside one de-margined probability space (TxLINE
StablePrice), so bookmaker margin doesn't contaminate the comparison (§2.4). Within a run, the
remaining differences are the strategies.

**Q10. What happens when TxLINE data is stale, gapped, or a close is suspended?**
Each has a designed honest path. Stale feed: feed-health telemetry (ops-only, never proof) and
deploy preflight fail-closed (§2.7, §11.2). Stream gap/interrupt: the partial run finalizes as an
explicit degrade with the cause in ops — sealed work is preserved, never relabeled true-CLV
(§3.4). Suspended/missing close: the law refuses to score (`closing_suspended`), the row is
invalid, and the run still verifies — demonstrated on real data (§12.3c). Recorder gaps are
explicit `gap` lines, never a silent splice (§2.5).

**Q11. Why not tune the detector until it fires on that fixture?**
Because that is precisely the fraud Veridex exists to prevent — fitting the strategy to the
evaluation data and presenting the result as skill. The protocol was one run, no tuning, no
fixture shopping; the config that ran is pinned by hash; and the roadmap the result motivates is
predeclared so future evaluation can't be retrofitted either (§12.2, §12.5). A tuned fire on one
known fixture would carry zero information about the next one.

**Q12. What breaks if I redeploy the same config?**
Nothing — and that's the point. The same submitted config yields the same `config_hash`
(canonical serialization, `deploy/preflight.py:115-125`); each deploy mints a fresh instance and
`run_id`, and identical inputs produce identical evidence bytes (§5.1). A backtest of the same
pack + window even pins the same run id (§12.1). Determinism is what makes "re-run it and check"
a real verification move.

**Q13. The LLM is in the loop somewhere — how do I know it never touches scoring?**
Three ways. Statically: the import audit forbids any LLM SDK in law/scoring/leaderboard/verifier/
checks/ingest/policy, and runs both as tests and as the live `llm_boundary` check that fails
closed if a trust directory is missing (§5.2). Structurally: the LLM's outputs enter only as a
constrained `AgentAction`, and its claims sit in a separate untrusted block (§4.1). Dynamically:
`metrics_recomputed` re-derives every displayed number from sealed evidence, so even a compromised
write path gets caught at verify time (§5.2).

**Q14. Your leaderboard could still be gamed by abstaining a lot, no?**
No. WAITs are excluded from CLV means (never counted as 0), so abstention doesn't inflate CLV;
zero-scored agents rank last (`avg_clv_bps=None` sorts after any number); and after the
confidence fix, abstentions can't even dress up the confidence tier — it keys off scored picks
(§4.2, §4.5). Meanwhile `valid_pct`/`valid_count` keep law-acceptance visible as its own honest
metric.

**Q15. What's a "window CLV" and why should I trust it less?**
You should — and the system makes sure you do. True CLV needs the real closing line, which only a
`pre_match` window (with a complete reconstructed close) has. Any other window closes on the
in-play line at window end, so its value is measured against a *different* reference — it's named
`window_clv_bps`, physically replaces `clv_bps` on the row, aggregates separately, and never
enters the rank axis (§3.2, §4.4). The mode label can't lie because the field name can't.

**Q16. Why Polymarket, and why is the client vendored?**
Polymarket is a real, liquid, programmatically-accessible venue with World Cup markets — the
minimum honest test of "real venue, really integrated" (read path live-verified against real
market structure, §8.3). The CLOB client is vendored, pinned, and MIT-licensed with provenance
documented in `veridex/venues/_vendor/README.md`: the money path depends on exact reviewed bytes
in-repo, not a floating pip release.

**Q17. What does `verified: true` actually mean — and what doesn't it mean?**
It means: the recomputed evidence hash over the sealed prefix matches the sealed hash — the
record wasn't altered. It does *not* by itself mean every check passed: the per-check block
carries the full verdict, and the UI renders "⚠ NOT fully verified" when a blocking check fails
with an intact seal. It never means "this strategy is profitable" — checks certify the record,
metrics rank performance, and the two never mix (§5.4, §14 row 1).

**Q18. If the venue lies about a fill, what happens?**
The receipt path is built to minimize trust in labels: fills are reconciled from the matched-size
*number* first (a positive match is a fill regardless of the status string), unknown states map to
honest `UNRESOLVED`, and an UNRESOLVED live receipt counts as an executed failure that moves the
circuit breaker (§8.1, §8.2). And whatever the venue reports, it is non-scoring — a lying receipt
could at worst mislabel execution telemetry, never a score (§5.5).

**Q19. Why is the demo synthetic — isn't that the cherry-picking you claim to prevent?**
The demo's job is to demonstrate the *pipeline* deterministically offline, and it says so on
every machine-readable surface: the pack self-declares `synthetic: true`, the caveat rides inline
with every CLV number, and an unmarked pack degrades to "unknown-provenance" rather than reading
as real (§7.5). The real-data result is published separately and honestly — including its zero
(§12). Cherry-picking is presenting the synthetic number as a real edge; the system makes that
structurally hard to do even by accident.

**Q20. What would it take for an insider to fake a winning run end-to-end?**
They would have to: forge sealed events whose recomputed hash matches (break SHA-256 or reseal),
make the law recompute produce the fake numbers from those events (i.e., forge a *consistent*
market history), keep the manifest binding and root-forest coherent, and — if anchored — do all
of it before the Memo committed, then survive any third party re-running `/runs/{id}/verify`
against the published evidence. The honest answer: an insider who controls the ingestion box
before sealing could feed fabricated *inputs* — which is why the TxLINE validation endpoint
(per-message Merkle proofs against TxLINE's own anchored root, `ingest/txline_client.py:107-132`)
exists in the design as the upstream authenticity hook, and why input authenticity is named
future scope rather than claimed solved.

---

## 18. Glossary

| Term | Definition |
|---|---|
| **AgentAction** | The constrained, frozen decision an agent may emit: an action type plus params (`runtime/schemas.py:25-34`). The only channel from agent to system. |
| **AgentInstance** | The durable, pinned deployment record: template + config + policy envelope + evidence links (`deploy/instance.py:78`). The instance IS the deployment. |
| **Anchor** | One Solana Memo transaction whose data is the run-manifest SHA-256 (`chain/anchor.py:75`). A commitment to the manifest hash — not a claim that every byte is on-chain. |
| **claimed_edge** | Whatever edge an agent asserts about its own pick. Recorded as untrusted metadata; never a scoring input (`law/recompute.py:155-157`). |
| **Closing policy (CON-040)** | The closing line is the last pre-`InRunning` odds update, reconstructed from `/odds/updates` (the pre-match snapshot is empty) (`ingest/txline_client.py:65`). |
| **CLV (closing-line value)** | `closing_prob_bps[side] − entry_prob_bps[side]` in the de-margined TxLINE probability space, recomputed from sealed entry vs close (`law/recompute.py:151-153`). The only scored/rank metric. |
| **clv_confidence** | Display-only sample-size tier (low ≤9 / medium ≤29 / high) keyed off **scored picks** (`clv_confidence.py:21`). Never a rank input. |
| **config_hash** | SHA-256 of the canonical serialization of a non-secret agent config; pins the exact behavioral surface of a deployed instance (`deploy/preflight.py:115`). |
| **Derived tail** | Every event outside the sealed prefix — law results, score updates, policy results, receipts, route telemetry — all `evidence=False` with `derived_from` refs (§3.3). |
| **Dry run vs paper** | Paper = no execution lane at all (proof-only). Dry run = the full policy/execution lifecycle with a simulated receipt from an offline fake venue. Both provably leave the sealed proof byte-identical (§5.5). |
| **evidence_hash** | SHA-256 over the canonically-serialized, sequence-ordered sealed RunEvents (`runtime/evidence.py:19`). run_id-independent: identical inputs ⇒ identical bytes. |
| **Executable edge** | Forward EV at the actual venue price for the size that submits: `round((p·price − 1)·10000)` bps (`law/edge.py:30`). Gates execution; never scored; renders only behind a real venue quote. |
| **FAK / FOK** | Fill-and-kill / fill-or-kill time-in-force. The only representable TIFs on this lane — GTC is unrepresentable by type (`venues/base.py:96`). |
| **Fair value** | TxLINE's de-margined, market-implied consensus probability (StablePrice `Pct`). Market-implied, not guaranteed truth; never re-de-vigged (§2.4). |
| **Kelly (capped fractional)** | Advisory sizing from the sealed law output: half-Kelly against a fixed bankroll, capped at `max_stake` (`execution/runner.py:162`). Policy sizing only; never a metric or rank input. |
| **law_valid_rate** | Law-acceptance fraction (valid decisions / total decisions, WAIT-inclusive) — the honest name for what replay law-validity measures (`backtest/report.py:180-183`). Distinct from scored coverage and from any policy pass rate. |
| **live_ready** | The preflight verdict that arms the live route: every boolean check passed AND the operator explicitly confirmed neg-risk approval AND the 1-share FAK smoke (`venues/polymarket_preflight.py:365-370`). Default false. |
| **Manifest** | The per-run binding record: run_id, fixture/window id, agent ids, evidence root, score root, proof-mode map, schema versions, root forest (`chain/anchor.py:13`). Its hash is the anchored payload. |
| **MarketState** | The frozen per-tick snapshot agents decide on: fixture, tick_seq, ts, phase, per-market `{stable_prob_bps, stable_price, suspended}` (`ingest/marketstate.py:15`). |
| **Mispricing gap** | `fair_prob_bps − venue_implied_prob_bps`: a probability-space dislocation, explanatory only — never an edge, never a score (`execution/legibility.py:41`). |
| **Neg-risk exchange** | Polymarket's separate exchange contract for negative-risk markets, requiring its own ERC-1155 approval — not offline-verifiable, hence operator-verified (§8.5). |
| **Operating curve** | The behavioral test tapes for a detector: null noise, injected sharp move, slow drift, single outlier, sustained repricing — each with an expected fire/quiet verdict (§10.4). |
| **Page-Hinkley** | A cumulative-sum change-point detector confirming a *sustained* level shift, returning its direction — up/down/None, never a bare bool (`strategies/sharp_stats.py:114`). |
| **pending (sentinel)** | The non-numeric `clv_bps` value for unscoreable-but-valid rows: WAIT, live awaiting close, pending_horizon. Excluded from CLV means; never a fabricated 0 (`law/recompute.py:45`). |
| **pending_horizon** | An entry strictly within `min_clv_horizon_s` of window close — too little runway to score; excluded like WAIT, and verifiable from the sealed window config (`runtime/window.py:90`). |
| **PolicyEnvelope** | The operator's committed guardrail set with a canonical `policy_hash` (`policy/envelope.py:18`). The execution boundary of an instance. |
| **Proof card** | The judge-visible artifact: run metadata, lineage, evidence block, the 7 checks, anchor status, and a separate metrics block (`verifier/proof_card.py:31`). |
| **Proof mode** | `reproducible` (deterministic rerun regenerates actions/scores) vs `verified` (recorded actions recomputed and checked; the LLM not byte-reproduced) vs `partial` (incomplete proof — shown, not rank-eligible) (`docs/faq.md`). An eligibility label, never a score. |
| **real_venue_quote** | A per-quote honesty flag earned only via an adapter's explicit `PROVIDES_REAL_VENUE_QUOTE` marker; the display gate for any edge number (§7.2-7.3). |
| **ReplayPack** | A self-describing, content-hashed directory of recorded raw TxLINE records + manifest + closing policy; refuses to replay if tampered (`ingest/replay_pack.py:23`). |
| **robust-z** | Median/MAD z-score (×1.4826) of the latest movement vs its own history, with a scale floor so a flat market's sudden repricing registers (`strategies/sharp_stats.py:83`). |
| **Root-forest** | Per-domain Merkle roots (event_log/score/receipt/policy/competition/payout_reserved) bound into the manifest before hashing (`chain/merkle.py:53`). |
| **RunEvent** | One sealed evidence record: `sequence_no`, `event_type`, and payload JSONs (`runtime/schemas.py:37`). The unit of the sealed prefix. |
| **RunWindow** | A live run's coverage frame: fixture, market allowlist, end rule, CLV horizon (`runtime/window.py:35`). Its effective config is sealed into evidence. |
| **Sealed prefix** | The RunEvent list covered by `evidence_hash` — ticks, decisions, errors, window config. Change one byte and `evidence_integrity` fails (§3.3, §5.1). |
| **StablePrice** | TxLINE's de-margined consensus odds product: `Pct` sums to ~100% across outcomes; the fair-value input for everything (§2.4). |
| **True CLV vs window-CLV** | True CLV is measured against the reconstructed real close (`pre_match` only); window-CLV against the line at window end (`window_clv_bps`) — separate fields, separate aggregates, never conflated (§3.2, §4.4). |
| **UNRESOLVED** | The honest terminal for an order whose fate is unknown at the receipt boundary (poll timeout). Never a guessed fill; counts as an executed failure for the breaker (§8.1). |
| **WAIT** | The agent's explicit abstention action — law-valid, never scored (`law/recompute.py:115-117`). |

---

*End of deep-dive. Companion documents: `README.md` (the product story), `docs/submission.md`
(the hackathon submission), `docs/operator-runbook.md` (the human-only real-money steps),
`docs/deploy-your-own-agent.md` (the SDK), `docs/txline-feedback.md` (integration findings),
`docs/faq.md` (product/trust Q&A).*
