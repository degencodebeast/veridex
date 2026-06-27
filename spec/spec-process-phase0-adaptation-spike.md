---
title: Phase 0 Adaptation Spike — Prove-or-Kill agent-rank Reuse for the TxLINE Agent Proof Arena
version: 0.3 (spike/decision spec — Codex R2 trust-language cleanup; READY TO RUN)
date_created: 2026-06-26
last_updated: 2026-06-26
owner: solo builder (TxODDS Agents track)
tags: [process, spike, proof-arena, txline, trust-boundary]
---

# Introduction

A time-boxed (1.5–2 day) **spike/decision spec** to **prove or kill** reusing the `agent-rank`
("Proof Arena V2") backend spine for the **TxLINE Agent Proof Arena**. The output is a **go/no-go
decision + a thin end-to-end proof-of-concept** (one fixture · one Agno/LLM agent · one
deterministic baseline · one proof card · one devnet anchor), **NOT** a production build. If the
spike fails its kill conditions, fall back to greenfield (option A) with lessons borrowed.

Authoritative parent: `.omc/plans/agents-arena-plan-v6.md` (v6.1). This spec realizes its
**Phase 0** (§7) and embeds its **9 TxLINE Proof/Trust gates** (§6).

## 1. Purpose & Scope

**Purpose.** De-risk the single most dangerous decision in the project — *"can `agent-rank`'s
verifier/Check/evidence/anchor machinery be adapted to TxLINE sports without fighting its DeFi
schema?"* — before committing to Phase 1.

**In scope:** the 6 reuse questions + the live-ingestion gate; one fixture; a thin
SSE→`MarketState` adapter; one Agno `AgentAction` agent; one deterministic baseline; a CLV Check
stub; a `VerifierRunResponse`-shaped proof card; one devnet Memo run-anchor.

**Out of scope (non-goals — must hold):**
- **NG-1:** No full tamper suite (one alter + one peek check max).
- **NG-2:** No multiple strategies / no leaderboard / no dashboard UI.
- **NG-3:** No real-money execution, no wallets, no Privy, no mainnet.
- **NG-4:** No production hardening (retries, scaling, auth refresh loops).
- **NG-5:** No reproduction depth, no anchor batching, no NL→DSL.
- **NG-6:** No rewrite of `agent-rank`'s DeFi check-modules (its "Cat" code) — adapt via *new* sibling **Check** modules only.

**Audience:** the solo builder (implementer) and Codex (smux reviewer).

**Assumptions:** `agent-rank/` is readable locally; TxLINE **World Cup 2026 free tier** on
**devnet** (`https://txline-dev.txodds.com/api/`) is reachable; Agno + a model provider key are
available; Solana devnet is reachable.

## 2. Definitions

| Term | Definition |
|---|---|
| **TxLINE** | TxODDS hybrid on-chain/off-chain sports data layer (consensus odds + scores), anchored on Solana. |
| **Stable Price** | TxLINE's consensus odds for European football — the **intended fair-value input**. *(Phase 0 confirms the payload field semantics — whether it is already de-vigged / probability-like — before Phase 1 relies on it.)* |
| **SSE** | Server-Sent Events; TxLINE live feed (`/api/odds/stream`, `/api/scores/stream`). |
| **CLV** | Closing-Line Value: StablePrice line movement from entry to a fixed horizon/close — the performance metric. |
| **MarketState(≤t)** | The immutable snapshot of TxLINE data up to tick `t` that an agent may see (no future rows). |
| **AgentAction** | The structured, constrained decision an agent emits (`{type, params}`, frozen). |
| **Proof Check** | A deterministic pass/fail verdict over recorded evidence, shown on the proof card. Named instances: **CLV Check · Evidence-Integrity Check · LLM-Boundary Check · Proof-Mode Check · Live-Input Check · Anchor Check · Replay-Consistency Check**. Branded umbrella = **Proof Checks**. *Reuses `agent-rank`'s internal "Cat" modules (`compute_*_cat`); we surface them as Checks — never expose "Cat" in tx-odds docs/UI/API.* |
| **Proof card** | The read-only `VerifierRunResponse` JSON (run·lineage·evidence·**Proof Checks**) — the judge-visible artifact. |
| **Evidence hash** | SHA-256 over deterministically-serialized, sequence-ordered run events. |
| **Memo manifest / anchor** | A Solana Memo tx whose payload is the hash of a run manifest — our per-run on-chain anchor. |
| **Txoracle** | TxLINE's own Anchor program; `validateStat` (read-only `.view()`) verifies a **score/stat** against daily Merkle roots (odds validation NOT assumed — see Gate 4 / §12). |
| **Proof mode** | `LLM / evidence-verified` (recorded+scored, not reproduced) vs `deterministic / reproducible` (re-run regenerates roots). |

## 3. Requirements, Constraints & Guidelines

**Spike gates (each is a YES/NO the spike must answer):**
- **REQ-001 (Q1):** `agent-rank`'s verifier/Check path ingests a **TxLINE sports evidence artifact** (a `MarketState`+`AgentAction` run feeding the `RunEvent`/hash pipeline).
- **REQ-002 (Q2):** a **CLV Check** (`compute_clv_check(db, run_id)`) can be added cleanly as a sibling Proof Check, reusing the same response shape.
- **REQ-003 (Q3):** one **Agno `AgentAction`** is emitted by an LLM agent **without entering the verifier/trust path** (import boundary holds — AST audit passes).
- **REQ-004 (Q4):** a **deterministic baseline** emits the **same `AgentAction` schema** (no Agno).
- **REQ-005 (Q5):** a **run hash/root is anchored on Solana devnet** via a **Memo manifest** with minimal work.
- **REQ-006 (Q6):** proof mode is shown in a **JSON proof-card stub** (no real UI in P0).
- **REQ-007 (live gate):** **live TxLINE SSE ingestion shares the same `MarketState`/evidence contract** as replay. *(Screening-critical: live input is mandatory for the track.)*

**Constraints:**
- **CON-001:** **No `agno`/`anthropic`/`openai`/`litellm` imports in the trust path** (`checks/`, `verifier/`) — enforced by AST audit (reuse `agent-rank`'s pattern).
- **CON-002:** **Devnet only.** No mainnet RPC, no real funds, no wallets.
- **CON-003:** **20–50 tick cap** for the spike replay window; do **not** anchor every tick (per-run Memo only).
- **CON-004:** **Deterministic serialization** for hashing: sorted keys, `separators=(",",":")`, sequence-ordered.
- **CON-005:** **Thin adapter only** — TxLINE SSE/replay must adapt INTO `agent-rank`'s evidence contract; do not rewrite the verifier/Check architecture.

**Guidelines:**
- **GUD-001:** Read-mostly; reuse `agent-rank` shapes wholesale (`VerifierRunResponse`, `AgentAction`, `RunEvent`, the hashing pattern).
- **GUD-002:** New behavior goes in **new sibling modules** (e.g. `checks/clv.py`, `ingest/txline_*`), never by mutating DeFi modules.
- **PAT-001:** `MarketState → [LLM/agent emits AgentAction] → deterministic law recomputes edge/CLV → RunEvent → evidence hash → proof card → Memo anchor`.

**KILL CONDITIONS → STOP, fall back to greenfield (A) with lessons borrowed:**
- **KILL-1:** `agent-rank`'s `RunEvent`/evidence schema is too DeFi-welded to carry a `MarketState`+`AgentAction` without broad rewrites.
- **KILL-2:** The Agno decision layer **cannot be isolated** from the verifier path (import boundary unachievable).
- **KILL-3:** A CLV Check **cannot be added** without broad rewrites of the verifier composition.
- **KILL-4:** On-chain run anchoring (Memo manifest) **cannot be re-added lightly** on devnet.
- **KILL-5:** Live SSE ingestion **cannot be adapted into the same evidence contract within one thin adapter**.
- **KILL-6:** The public proof card **cannot expose `checks` / Proof Checks without leaking `cats`
  vocabulary or requiring broad verifier-schema rewrites** (a thin response adapter over
  `VerifierRunResponse.cats` should suffice; if it doesn't, that's a reuse-friction "thin-adapter-vs-rewrite" signal).
*(Note: `validateStat` granularity is **claim-strengthening, NOT a kill** — if unusable, ship G1-only recorded-evidence with an honest claim downgrade.)*

## 3.5 TxLINE Proof / Trust Boundary Gates (MANDATORY — from v6 §6; each with an acceptance check)

1. **LLM self-certification** — LLM may propose+explain, may not certify edge/CLV/Kelly/proof/pass-fail.
   *Check:* LLM claims a favorable edge but deterministic recompute disagrees → verifier rejects/downgrades.
2. **Deterministic law** — fair-value/edge/CLV/validation/proof/scoring recomputed from evidence.
   *Check:* verifier/scorer/Check tests run with **no `agno`/LLM imports** (AST audit).
3. **Evidence before scoring** — bind a **raw pre-score record** (evidence-snapshot hash + raw
   `AgentAction` + schema version + agent identity + **model/prompt/config hash** + tick/order +
   proof mode) created *before* the score row; verifier scores ONLY from it.
   *Check:* a post-hoc modified evidence/action bundle changes the hash and fails verification.
4. **On-chain vs off-chain claim** — state exactly what is anchored vs hashed off-chain vs local.
   **Required on-chain proof for the spike = (b) our Memo manifest** anchoring the *agent run record*.
   (a) **TxLINE-native `validateStat`** authenticates **score/stat** inputs — *claim-strengthening
   ONLY when the evidence uses score stats AND Phase 0 confirms the proof path*. **Odds/StablePrice
   inputs are recorded off-chain evidence** unless Phase 0 confirms an odds-specific validation path
   (a `validateStat`-for-odds is NOT assumed — the opened TxLINE on-chain-validation docs cover scores).
   *Check:* README/demo copy cannot claim on-chain anchoring (or TxLINE-native authentication of
   *odds*) for anything not actually anchored/proven — `test_no_validate_stat_claim_for_odds_without_confirmed_odds_proof`.
5. **Replay vs live mode** — replay proves performance; **live proves the track's live-input requirement**.
   *Check:* spike shows a live TxLINE SSE connection producing the same `MarketState` as replay.
6. **Proof-mode labels** — `LLM / evidence-verified` vs `deterministic / reproducible`.
   *Check:* proof-card JSON exposes proof mode per run; no field blurs both into one claim.
7. **Runtime / tool boundary** — Agno agents emit only `AgentAction` (`tools=[]`); cannot mutate
   proof records, write verifier state, fetch untracked evidence, or bypass validation.
   *Check:* tests prove verifier path runs without Agno and rejects malformed/overpowered actions.
8. **Demo artifact** — a proof card showing TxLINE evidence → `AgentAction` → recomputed edge/CLV →
   verifier result → proof-mode label → anchor status.
   *Check:* the JSON stub renders all six elements for one real fixture.
9. **Scope / non-goals** — see NG-1..NG-6 (no real-money, no mainnet, no marketplace, no free-form
   LLM control post-registration, no breadth before the slice works).

## 4. Interfaces & Data Contracts

### 4.1 TxLINE ingest (EXT)
- **Live:** `GET /api/odds/stream`, `GET /api/scores/stream` (SSE). Data messages: `id="timestamp:index"`,
  `data={single odds/score record}`; heartbeats `event: heartbeat`, `data:{"Ts":...}`.
- **Replay:** `GET /api/odds/updates/{epochDay}/{hour}/{interval}` (5-min historical intervals);
  `GET /api/scores/snapshot/{fixtureId}?asOf=...`. File-order canonical for determinism.
- **Auth:** guest JWT (`/auth/guest/start`, 30-day) + API token; headers `Authorization: Bearer {jwt}`,
  `X-Api-Token: {apiToken}`. Devnet base `https://txline-dev.txodds.com/api/`.
- **Soccer encoding:** phases NS=1,H1=2,HT=3,H2=4,F=5…; stat keys 1/2 goals, 3/4 yellow, 5/6 red,
  7/8 corners; period key `(period*1000)+base`.

### 4.2 `MarketState(≤t)` (NEW; the immutable snapshot the agent sees)
```python
# immutable view — NO future rows, NO now(), NO network inside the agent
MarketState = {
  "fixture_id": int, "tick_seq": int, "ts": int, "phase": int,
  "markets": { market_key: {"stable_prob_bps": int, "stable_price": float, "suspended": bool} },
  "scores": { stat_key: int },          # e.g. {1: home_goals, 2: away_goals, ...}
}
```

### 4.3 `AgentAction` (sports — adapt `agent-rank/backend/src/db/schemas.py:24–76`)
```python
class SportsActionType(str, Enum):
    WAIT="WAIT"; FLAG_VALUE="FLAG_VALUE"; FOLLOW_MOMENTUM="FOLLOW_MOMENTUM"
    FADE="FADE"; WIDEN_OR_SUSPEND="WIDEN_OR_SUSPEND"
class AgentAction(BaseModel):       # frozen=True
    type: SportsActionType
    params: dict[str, Any]          # e.g. {market, side, reason, confidence}  (reason/confidence = UX, NOT scored)
```
Emitted by Agno: **preferred** `Agent(model=..., tools=[], output_schema=AgentAction)` →
`agent.run(state).content`; **fallback** prompt-contract + JSON-parse → validated `AgentAction`
(`agent-rank` defaults `use_output_schema=False` because a prior OpenRouter model failed with it —
accept *either* path; **kill only if neither** produces a schema-valid action without entering the trust path).
Deterministic baseline emits the same schema with no LLM.

### 4.4 CLV Check (NEW sibling — mirror `cats/wallet_safety.py:192` signature)
```python
async def compute_clv_check(db, run_id: int) -> SportsClvCheckResponse:
    # result: Literal["pass","fail"]; checks: list[Check]; reason: str|None; evidence: ...
    # NO agno/openai/anthropic imports (AST-audited, gate 2)
```
Rules (stub set): `clv_horizon_present`, `entry_devig_identity`,
`recomputed_edge_matches` (LLM-claimed edge IGNORED — gate 1), `proof_mode_label`.

### 4.5 Evidence + hashing (reuse `agent-rank` pattern verbatim)
- `RunEvent{sequence_no, event_type, state_snapshot_json, action_payload_json, validation_payload_json, result_payload_json}`.
- `serialize_payload`: `json.dumps(..., sort_keys=True, separators=(",",":"))`.
- `compute_evidence_hash`: SHA-256 over `sorted(events, key=sequence_no)`.
- **Raw pre-score record (gate 3):** `{evidence_hash, raw AgentAction, action_schema_version, agent_id, model_prompt_config_hash, tick_seq, proof_mode}` written BEFORE any score row.

### 4.6 Proof card (reuse `VerifierRunResponse` shape)
`{verifier_version:"v0", run{run_id,status,run_log_hash,action_schema_version,evidence_schema_version,…},
lineage{…,proof_mode}, evidence{run_log_hash,run_event_count,verification_artifacts[]}, checks{clv,…}}`.
**Note (not "reuse wholesale"):** `agent-rank`'s `VerifierRunResponse` field is named **`cats`** — the
public proof card needs a **thin response adapter** (or schema fork) to surface it as `checks` /
**Proof Checks** without exposing "Cat" (test `test_proof_card_public_json_uses_checks_not_cats`).

### 4.7 On-chain anchor (devnet)
- **Our anchor:** one Solana **Memo tx per run**, payload = SHA-256 of a run manifest
  `{run_id, fixture_id|live_window_id, agent_ids, action_evidence_root, score_root, proof_mode_map, code_prompt_schema_versions}`.
  (Lighter than re-adding `finalize_run(run_log_hash:[u8;32])`; both options confirmed available.)
- **TxLINE-native authenticity (claim-strengthening — SCORES only):** `Txoracle.validateStat(...).view()`
  over Merkle proofs from `/api/scores/stat-validation` authenticates **score/stat** inputs — used ONLY
  if Phase 0 confirms the path. **Odds/StablePrice = recorded off-chain evidence** unless an
  odds-specific validation path is confirmed (do NOT claim it). The spike's required on-chain proof is
  the Memo manifest above.

## 5. Acceptance Criteria

- **AC-001 (Q1):** Given a recorded `MarketState`+`AgentAction` run, When fed through the
  `RunEvent`/`compute_evidence_hash` pipeline, Then a stable `run_log_hash` is produced and a
  proof card renders.
- **AC-002 (Q2):** Given `compute_clv_check(db, run_id)`, When run on the recorded run, Then it
  returns `result ∈ {pass, fail}` with a rules list, in `agent-rank`'s internal verifier-block shape (`VerifierCatsBlock`), adapted publicly as `checks`.
- **AC-003 (Q3):** Given the Agno agent module, When the import-boundary AST test runs, Then no
  `agno`/LLM import exists in `checks/`/`verifier/`, and the verifier imports cleanly with agno absent.
- **AC-004 (Q4):** Given the deterministic baseline, When run on the same `MarketState` stream twice,
  Then it emits identical `AgentAction`s (reproducible) using the same schema as the Agno agent.
- **AC-005 (Q5):** Given a completed run, When the Memo anchor step runs on devnet, Then a Memo tx
  signature is returned and its payload equals the run-manifest hash.
- **AC-006 (Q6):** Given a completed run, When the proof-card JSON is emitted, Then it exposes
  `proof_mode` and all six demo-artifact elements (gate 8).
- **AC-007 (live gate):** Given the live SSE stream, When connected for ≥1 fixture window, Then it
  yields the **same `MarketState` shape** as replay (one adapter, gate 5).
- **AC-008 (gate 1 enforcement):** Given an `AgentAction` whose claimed edge ≠ the deterministically
  recomputed edge, When scored, Then the action is rejected/downgraded and the record shows it.

## 6. Test Automation Strategy

- **Language/framework:** **Python 3.11 + `pytest`** (override the template's .NET examples).
- **Named trust-boundary tests (gate enforcement):** `test_verifier_imports_without_agno`,
  `test_llm_claimed_edge_is_ignored`, `test_action_with_untracked_market_state_rejected`,
  `test_malformed_or_overpowered_action_rejected`, `test_score_uses_recomputed_values_not_action_payload`.
- **Determinism:** `test_marketstate_replay_is_deterministic`, `test_evidence_hash_stable_cross_process`.
- **Import boundary:** reuse `agent-rank`'s AST-walk audit over the new `checks/`/`verifier/` modules.
- **Live/replay parity:** `test_live_and_replay_yield_same_marketstate_shape`,
  `test_live_stream_parser_tolerates_plain_lines_and_sse_heartbeat_if_present`.
- **Evidence / anchor / claim (Codex spec-review additions):**
  `test_raw_prescore_record_written_before_score_row`, `test_memo_manifest_hash_matches_anchor_payload`,
  `test_run_manifest_includes_code_prompt_schema_versions`, `test_proof_card_public_json_uses_checks_not_cats`,
  `test_no_validate_stat_claim_for_odds_without_confirmed_odds_proof`,
  `test_agno_output_schema_fallback_json_parse_produces_agent_action`.
- **Coverage:** spike-level — one happy path + one rejection per gate; not full coverage.
- **CI:** local `pytest` run; no pipeline required for the spike.

## 7. Rationale & Context

A spike (not a build) because Phase 0 is the highest-risk decision: reuse vs greenfield hinges on
whether `agent-rank`'s evidence/verifier/anchor shapes survive a TxLINE-sports load. Research
*de-risked* three unknowns: live = **SSE** (gate 5 achievable); StablePrice is the **intended
fair-value input** (Phase 0 confirms payload semantics before Phase 1 relies on it); **devnet** +
**Txoracle `validateStat`** give a native on-chain authenticity layer **for SCORES**
(claim-strengthening, *not* odds) on top of our **Memo anchor** — which is the spike's *required*
on-chain proof. The 9 gates are the trust contract; the kill conditions bound the spike so it can't
become an open-ended refactor.

## 8. Dependencies & External Integrations

### External Systems
- **EXT-001:** TxLINE off-chain API (devnet `txline-dev.txodds.com`) — SSE streams + replay + auth.
- **EXT-002:** `agent-rank` repo — reuse source (verifier/Check/evidence/runtime/Anchor schemas).
- **EXT-003:** Solana devnet + **Txoracle** program (`validateStat` view; daily Merkle roots).

### Third-Party Services
- **SVC-001:** Agno SDK + an LLM provider (OpenRouter/OpenAI/Anthropic) — decision layer only.

### Technology Platform Dependencies
- **PLT-001:** Python 3.11 / FastAPI / SQLite (spike DB); `@coral-xyz/anchor` + `@solana/web3.js` (or a Python Solana client) for the Memo tx + `validateStat`.

### Compliance / Security Dependencies
- **COM-001:** Devnet-only; no real-money wagering (track T&C). **SECURITY: do NOT copy
  `agent-rank/privy_authorization_private.pem`; no Privy/wallets in the spike.**

## 9. Examples & Edge Cases

```jsonc
// AgentAction (LLM-emitted; reason/confidence are UX, NOT scored)
{ "type": "FLAG_VALUE",
  "params": { "market": "GER_ECU_OU_2_5", "side": "OVER",
              "reason": "StablePrice 58% vs lagged 52%", "confidence": 0.66 } }

// Proof card stub (one fixture)
{ "verifier_version": "v0",
  "run": { "run_id": 1, "status": "complete", "run_log_hash": "ab12…", "action_schema_version": "sports_v0" },
  "lineage": { "proof_mode": "LLM/evidence-verified" },
  "evidence": { "run_log_hash": "ab12…", "run_event_count": 30, "verification_artifacts": [{ "artifact_type":"memo_anchor","content_hash":"…" }] },
  "checks": { "clv": { "result": "pass", "rules": [ {"id":"recomputed_edge_matches","passed":true} ] } } }
```
**Edge cases the spike must handle:** SSE **heartbeat** messages (ignore); **suspended market**
(`TXCS`/`suspended:true` → no signal, still recorded); **stale line** (no movement → `WAIT`);
**JWT 401** (re-acquire); **deterministic baseline tie** (explicit `WAIT`).

## 10. Validation Criteria

**Go/no-go decision rule:** if **all** of REQ-001..007 are answered YES (within the thin-adapter
constraint) → **proceed with C** (write the Phase 1 spec). If **any KILL-1..6** triggers → **fall
back to greenfield A**, carrying the reusable lessons (schemas, hashing pattern, gate set).
**Handoff:** the spike report + this spec are reviewed via **smux Codex** (and optionally
`$proof-arena-spec-factory review`) before any Phase 1 code.

## 11. Related Specifications / Further Reading

- `.omc/plans/agents-arena-plan-v6.md` (v6.1 — parent plan; §6 gates, §7 phases).
- `.omc/reviews/agents-arena.md` (decisions packet) · `.omc/reviews/v6.codex.md` (v6 review).
- `agent-rank/` — `backend/src/integrity/{verifier,cats}/`, `backend/src/runtime/`, `programs/agent_arena/`.
- TxLINE docs: `txline-docs.txodds.com` (odds/scores SSE, soccer-feed, on-chain-validation, OpenAPI).
- Agno docs: `docs.agno.com` (`output_schema`, AgentOS).

## 12. Research Findings (fact / inference / recommendation)

**Local — `agent-rank` (FACT):** `AgentAction` Pydantic schema (`{type, params}`, frozen); `RunEvent`
`{state_snapshot_json, action_payload_json, validation_payload_json, result_payload_json}`; deterministic
`compute_evidence_hash` (sorted-by-seq SHA-256); `VerificationArtifact.content_hash`; `VerifierRunResponse`
with a **`cats`** block; verifier composes deterministic "Cat" modules; no-LLM-import AST audit; AgentOS
wrapper defaults `use_output_schema=False` with JSON-parse fallback; `agent_arena` Anchor
`finalize_run(run_log_hash:[u8;32])`; V2 hosted runs skip anchoring.
*INFERENCE:* the evidence/verifier/anchor shapes can carry a `MarketState`+`AgentAction` run; a CLV Check
fits as a sibling. *RECOMMENDATION:* reuse via thin sibling modules + a response adapter; never mutate DeFi modules.

**TxLINE official docs (FACT):** devnet `txline-dev.txodds.com` + mainnet bases; `GET /api/odds/stream` +
`GET /api/scores/stream` (SSE); auth `Authorization: Bearer` + `X-Api-Token`; the on-chain-validation page
covers **scores** via `/api/scores/stat-validation` + `Txoracle.validateStat(...).view()`; soccer
phase/stat-key + `(period*1000)+key` encodings; free WC tier ~60s sampling.
*INFERENCE:* live-input gate is achievable; on-chain authenticity is confirmed for **scores**, not yet **odds**.
*NOT CONFIRMED in the opened pages — treat as observations to handle, not assumptions:* exact SSE event shape
(`id="timestamp:index"`, heartbeat); that StablePrice is already de-vigged/probability-shaped; an odds-specific
`validateStat` path. *RECOMMENDATION:* Phase 0 confirms StablePrice payload semantics + any odds-proof path; the
parser tolerates raw lines/heartbeats.

**Agno docs — Context7 (FACT):** `Agent(..., output_schema=PydanticModel)`; `response.content` is the typed
instance; `tools=[]` for decision-only. *INFERENCE:* suitable as the constrained decision layer.
*RECOMMENDATION:* prefer `output_schema`; keep a JSON-parse fallback (provider-routing caveat).
