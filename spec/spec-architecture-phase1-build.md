---
title: Veridex Phase 1 — Provable Agent Competition (1A Screening + 1B Proof-Polish)
version: 0.2
date_created: 2026-06-27
last_updated: 2026-06-27
owner: degencodebeast
tags: [architecture, process, agents, solana, txline]
---

# Introduction

Phase 0 proved (verdict GO) that the agent-rank "Proof Arena V2" spine reuses for TxLINE sports.
Phase 1 turns that spine into a **working, provable competition**: at least one LLM-powered agent plus
a deterministic baseline act on TxLINE markets, a deterministic law recomputes and scores them by
closing-line value (CLV), and each run produces a tamper-evident, proof-labeled record anchored on
Solana devnet. This spec defines the
requirements, interfaces, and acceptance criteria for Phase 1, split into **1A (screening — make the
loop run end-to-end)** and **1B (proof-polish — make the output provable and rankable)**.

## 1. Purpose & Scope

**Purpose.** Specify a build that takes Veridex from "spine proven offline + live spikes" to "agents
compete on a TxLINE fixture/window, get scored by a deterministic law, and the run is anchored and
shown on a leaderboard — all with honest proof labels."

**In scope (1A — Screening):**
- Production TxLINE **odds** ingestion: native-message normalizer + a live SSE client. (Scores stream is best-effort/deferred — see REQ-102 and non-goals; Phase 1 scores CLV off odds only.)
- Agent loop: an LLM agent (Agno) emits a constrained `AgentAction`; a deterministic baseline agent.
- Deterministic law: recompute edge / CLV / Kelly-fraction / validation from evidence only.
- Run orchestration + persistence: drive ingest → agent → law → evidence → score for one run.
- Scoring: rank a run's agents by recomputed CLV.

**In scope (1B — Proof-polish):**
- Proof card enrichment (verifier version, schema versions, proof-mode map, anchor status).
- Package anchor wiring: ONE devnet Memo per run over the run-manifest hash (`anchor_memo`).
- Evidence-hash hardening (duplicate-`sequence_no` safety, canonical-array hashing).
- Leaderboard: rank agents across runs from proof-labeled records.
- Competition harness: ≥2 agents on the same fixture/window, scored together.

**Out of scope (Phase 2+ / non-goals):**
- The on-chain `agent_arena` Anchor program, on-chain settlement/escrow, real-money staking.
- Privy hosted wallets / multi-user auth.
- A full historical replay client (Phase 1 uses small fixtures + live windows).
- Production UI beyond a minimal read-only leaderboard/proof view (JSON/static acceptable).
- TxLINE `validateStat` Merkle-proof execution (labeling boundary only; live proof is later).
- **Scores-stream normalization into `MarketState.scores`** — deferred. Phase 0 observed only a `{Ts}`
  heartbeat; Phase 1 scores CLV off odds. `scores` stay empty unless a useful schema is observed.
- **Multiple distinct LLM agents** — Phase 1 ships ≥1 LLM agent + the deterministic baseline; a second
  LLM strategy is a later add (do not build before the 1A loop is green).

**Audience.** Implementing subagents and reviewers (Claude + Codex).

## 2. Definitions

- **MarketState(≤t):** immutable snapshot of TxLINE data up to tick t an agent may see (no future rows).
- **AgentAction:** constrained decision `{type, params}` the agent emits; `params` reason/confidence are UX only, never scored.
- **Deterministic law:** pure-rules recomputation of edge/CLV/Kelly/validation from evidence; the LLM never self-certifies.
- **CLV:** closing-line value — the scoring signal. Compares the agent's entry `stable_prob_bps[side]`
  for a market against the **ClosingSnapshot**'s `stable_prob_bps[side]` for the same market key (see §4).
- **ClosingSnapshot:** the `MarketState` at the closing horizon H for a market key. Phase-1 replay: H = the
  final tick in the committed fixture/window (unless explicitly overridden). Live mode: CLV is `pending`
  until a later horizon tick exists.
- **source_mode:** per-run label `replay` | `live` (carried on `RunResult` + leaderboard rows; CON-005).
- **Run:** one competition instance over a fixture or live window; produces RunEvents, score rows, a manifest, and ONE anchor.
- **Proof mode:** `LLM/evidence-verified` (LLM agents) or `reproducible` (deterministic agent).
- **Native message:** a raw TxLINE odds SSE message (`FixtureId, Ts, SuperOddsType, MarketPeriod, MarketParameters, PriceNames, Prices, Pct, InRunning`).
- **StablePrice:** TxLINE's de-margined consensus odds (Phase 0 confirmed: `Pct` sums ~100%, `Prices` = decimal×1000).

## 3. Requirements, Constraints & Guidelines

### 1A — Screening
- **REQ-101**: A production normalizer folds N native odds messages for ONE fixture into a `MarketState` (market key = `SuperOddsType|MarketPeriod|MarketParameters`; `Pct`→`stable_prob_bps`, NA-tolerant; `Prices`→`stable_price`; `InRunning`→phase), living in `veridex/ingest/`.
- **REQ-102**: A live SSE client streams `/api/odds/stream` with `Authorization: Bearer {jwt}` + `X-Api-Token`, parses via the existing `parse_sse_line`, normalizes via REQ-101, and yields `MarketState`s. JWT is minted from the calling host (IP-bound) and read from local env, never committed. The scores stream is best-effort/optional (subscribe if present, but `MarketState.scores` may stay empty — see non-goals). **Live input is the screening gate, not polish:** Phase 1A is not demo-complete until the REQ-102 offline tests pass AND the creds-gated live smoke has been run and recorded.
- **REQ-103**: An LLM agent emits a validated `AgentAction` via Agno (`Agent(tools=[], output_schema=AgentAction)` with the JSON-parse fallback). The Agno call lives OUTSIDE the import-audited trust path.
- **REQ-104**: The deterministic law recomputes, from evidence only: recomputed edge (bps), CLV (per the ClosingSnapshot contract in §4), a Kelly fraction (advisory/risk-sizing — **not** a score axis in Phase 1, capped to [0,1]), and action validation (with a reason code). Reuses/extends the existing CLV Check. No LLM-claimed value is trusted. The CLV/closing contract (§4) MUST be settled before B3 is implemented.
- **REQ-105**: A run orchestrator drives ingest → agent decision → law recompute → evidence record → score row for a sequence of ticks, for ≥2 agents (≥1 LLM + the deterministic baseline), and persists RunEvents + score rows (`veridex/store.py`).
- **REQ-106**: Scoring ranks a run's agents by recomputed CLV (deterministic, reproducible from evidence).

### 1B — Proof-polish
- **REQ-111**: The proof card includes `verifier_version`, schema versions, the proof-mode map, and anchor status, while keeping public `checks` (never `cats`).
- **REQ-112**: `anchor_memo` is implemented in the package (live solders), sending ONE devnet Memo per run whose payload == the run-manifest hash; the run record stores the tx signature.
- **REQ-113**: Evidence hashing rejects (or disambiguates) duplicate `sequence_no` and uses a canonical array hash; cross-process stable.
- **REQ-114**: A leaderboard ranks agents across runs from the proof-labeled score records, exposing proof mode + anchor status per entry.
- **REQ-115**: A competition harness runs ≥2 agents on the same fixture/window and produces a single comparable, scored, anchored run.

### Constraints (the 9 TxLINE Proof/Trust gates — guardrails)
- **CON-001 (LLM-self-cert)**: the LLM never certifies its own result; the law scores from evidence.
- **CON-002 (deterministic-law)**: all scoring math is pure-rules and reproducible.
- **CON-003 (evidence-before-scoring)**: a bound raw pre-score record exists before any score row.
- **CON-004 (on-chain-vs-off-chain claim)**: odds = recorded evidence; scores = `validateStat`; confirmed-odds ≠ `validateStat`. No overclaim.
- **CON-005 (replay-vs-live)**: replay and live share the same `MarketState` contract; runs label which they used.
- **CON-006 (proof-mode-labels)**: every record carries `LLM/evidence-verified` or `reproducible`.
- **CON-007 (runtime/tool-boundary)**: the trust path (`checks/`, `verifier/`, scoring, ingest) imports no LLM SDK; enforced by the import audit.
- **CON-008 (demo-artifact)**: every claim in the demo maps to a record/tx/test.
- **CON-009 (scope/non-goals)**: no on-chain arena program, no real-money staking, no Privy in Phase 1.

### Guidelines
- **GUD-001**: TDD (Iron Law) for every bit — failing test watched first.
- **GUD-002**: Offline tests use committed fixtures modeled on live-confirmed schema; live calls are a separate, creds-gated smoke, never required for the suite to pass.
- **GUD-003**: Live market data and secrets (`veridex/.env`) are never committed.
- **GUD-004**: Each bit ships behind the subagent-driven flow (implementer → spec-compliance review → code-quality review) + a codex gate.

## 4. Interfaces & Data Contracts

```
veridex/ingest/txline_normalize.py
  market_key(message) -> str
  group_by_fixture(messages) -> dict[int, list[message]]
  marketstate_from_txline_odds(messages, *, tick_seq=0) -> MarketState   # single fixture; raises on mixed
veridex/ingest/live_client.py
  stream_marketstates(*, fixture_id|window, creds) -> Iterator[MarketState]   # auth + parse_sse_line
veridex/runtime/agent.py
  emit_agent_action(market_state, *, prefer_output_schema=True) -> AgentAction  # Agno, outside trust path
veridex/law/  (deterministic law — trust path, LLM-free)
  recompute(entry_state, action, *, closing) -> {edge_bps, clv_bps|"pending", kelly_fraction, valid: bool, reason: str}
veridex/runtime/orchestrator.py
  run_competition(marketstates, agents, *, source_mode) -> RunResult   # RunEvents + score rows + manifest + source_mode
veridex/scoring.py
  score_run(run) -> [ {agent_id, clv_bps, rank, proof_mode} ]
veridex/chain/anchor.py
  anchor_memo(manifest_hash) -> tx_signature        # ONE devnet Memo per run (REQ-112)
veridex/verifier/proof_card.py
  build_proof_card(...) -> {verifier_version, run, lineage{proof_mode_map, schema_versions}, evidence, checks, anchor}
veridex/leaderboard.py
  leaderboard(records) -> [ {agent_id, agg_clv, runs, proof_mode, anchor_status, source_mode} ]
```

Data contract — normalized market value (per market key):
```json
{ "stable_prob_bps": {"over": 4684, "under": 5316}, "stable_price": {"over": 2.135, "under": 1.881}, "suspended": false }
```

Data contract — CLV / ClosingSnapshot (settles REQ-104 / B3):
```text
AgentAction.params MUST include `market_key` and `side` for any non-WAIT action that is to be scored.
ClosingSnapshot = the MarketState at horizon H for that market_key:
  - replay: H = the final tick of the committed fixture/window (override allowed via run config).
  - live:   CLV is "pending" until a tick later than entry exists for that market_key.
clv_bps = closing.stable_prob_bps[side] - entry.stable_prob_bps[side]   # de-vigged prob, in bps
Invalid / unscored (valid=false, with reason code) when at entry OR closing the market_key is:
  absent | suspended | missing the side | Pct="NA" (no stable_prob_bps[side]).
The LLM-claimed edge is NEVER used; clv_bps/edge_bps come only from recomputed stable_prob_bps.
```

## 5. Acceptance Criteria

- **AC-101**: Given native messages for one fixture, When normalized, Then a `MarketState` with the correct market keys, de-vigged `stable_prob_bps` (multi-outcome sums ~10000), scaled `stable_price`, and NA→suspended is produced; mixed-fixture input raises.
- **AC-102**: Given valid creds, When the live client runs against devnet, Then ≥1 real `MarketState` is yielded (creds-gated smoke, not in the offline suite).
- **AC-103**: Given a `MarketState`, When the LLM agent runs, Then a schema-valid `AgentAction` is returned, and `veridex/law` + `verifier`/`checks` import no LLM SDK (audit passes).
- **AC-104**: Given an action with an inflated claimed edge, When the law recomputes, Then the score uses the recomputed edge/CLV only (claim ignored). And per the §4 CLV contract: positive CLV from entry-vs-final-tick; missing closing market → `pending` (live) / invalid (replay); missing `side` → invalid; suspended or `Pct="NA"` at entry or closing → invalid/unscored with a reason code; live CLV stays `pending` until a later horizon tick exists.
- **AC-105**: Given a run over ≥2 agents, When orchestrated, Then RunEvents + score rows persist and a raw pre-score record precedes every score row (CON-003).
- **AC-106**: Given a completed run, When scored, Then agents are ranked by recomputed CLV deterministically (same run → same ranking).
- **AC-111**: The proof card JSON contains `verifier_version`, schema versions, proof-mode map, and anchor status; `checks` present, `cats` absent.
- **AC-112**: A run produces exactly ONE devnet Memo tx whose payload == the run-manifest hash; the signature is stored and verifiable on-chain.
- **AC-113**: Evidence hash is identical across processes and stable under input reordering with unique `sequence_no`; duplicate `sequence_no` is rejected or deterministically disambiguated.
- **AC-114**: The leaderboard ranks agents across ≥2 runs and shows proof mode + anchor status per entry.
- **AC-115**: A competition of ≥2 agents on one fixture yields one scored, anchored run comparing them.

## 6. Test Automation Strategy

- **Levels**: unit (each module), integration (a full run end-to-end on a fixture), creds-gated live smoke (ingest + anchor).
- **Framework**: pytest (Python 3.11), `uv`-managed venv.
- **Data**: committed JSON fixtures modeled on the T9-confirmed native schema; NO live market data committed.
- **Determinism**: no `now()`/network in unit tests; live calls isolated behind explicit smoke scripts/markers.
- **Trust audit**: the import-boundary AST audit runs as a standing test over `checks/`, `verifier/`, `law/`, `ingest/`, `scoring`.
- **Coverage**: every REQ maps to ≥1 AC and ≥1 test; no `skip`/`xfail` placeholders count as done.

## 7. Rationale & Context

Phase 0 confirmed the load-bearing facts: StablePrice is de-margined consensus (so CLV/edge math plugs
in), the native schema needs a thin normalizer, the JWT is IP-bound, and a Memo anchor confirms in
~1.3s. Phase 1 turns these into a real competition while keeping the Phase-0 honesty discipline: the
LLM proposes, the deterministic law disposes, and every claim maps to a record/tx/test. 1A is split
from 1B so a *running* loop exists before *polish*, de-risking the demo.

## 8. Dependencies & External Integrations

- **EXT-001**: TxLINE devnet API (`txline-dev.txodds.com`) — odds SSE (scores best-effort/deferred) + auth (guest JWT + apiToken via on-chain subscribe, already obtained in Phase 0).
- **SVC-001**: An LLM provider (Agno + a model key) — for `emit_agent_action`; outside the trust path.
- **INF-001**: Solana devnet RPC + a funded keypair (treasury, Phase 0) — for the Memo anchor.
- **PLT-001**: Python ≥3.11; `solders`/`solana` (anchor), `agno` (agent), `pydantic` (schemas).
- **DAT-001**: TxLINE native odds/scores messages — schema fixed in Phase 0 (`scripts/txline_live/` evidence).

## 9. Examples & Edge Cases

```text
- NA outcomes: a market whose Pct is all "NA" => empty stable_prob_bps + suspended=True.
- Mixed-fixture batch passed to the single-fixture normalizer => ValueError.
- Duplicate sequence_no in an evidence bundle => rejected/disambiguated (REQ-113).
- A live (InRunning=true) message => phase=live; a pre-match batch => phase=pre-match.
- An LLM that returns a fat claimed edge => ignored; recomputed edge governs the score.
```

## 10. Validation Criteria

- All ACs have passing tests; the full offline suite is green and contains no skips.
- The import audit passes over the whole trust path including new modules.
- One integration test runs a full ≥2-agent competition on a fixture and asserts ranking + proof card + (mocked) anchor.
- Creds-gated live smoke (ingest + one real Memo anchor) succeeds when run manually.
- Codex review signs off the spec, the plan, and each implemented bit with no remaining overclaim.

## 11. Related Specifications / Further Reading

- `spec/spec-process-phase0-adaptation-spike.md` — the Phase 0 spike spec.
- `.omc/plans/agents-arena-plan-v6.md` — the v6 build plan (Phase 1 1A/1B split, 9 gates, leaderboard).
- `.omc/reviews/phase0-result.md` — Phase 0 verdict + empirical findings + Phase-1 carry-list.
