# Veridex — System Architecture

> How everything works together: components, data flow, trust boundaries, and the diagrams that
> tie them into one picture. This is the companion to the
> [technical deep-dive](technical-deep-dive.md), which explains every design decision in depth
> with file-and-line references — this document is the map; the deep-dive is the territory.

**Honesty note (read first).** The diagrams below show the *built* system. Where a path exists in
code but has never been exercised with real money (the Polymarket write path), or is designed but
not wired (custody/payouts), the diagram and the text say so. No real-money order has ever been
placed; the live path is fail-closed and operator-only by construction.

---

## Contents

1. [The one-paragraph architecture](#1-the-one-paragraph-architecture)
2. [The big picture (system diagram)](#2-the-big-picture-system-diagram)
3. [Layer responsibilities](#3-layer-responsibilities)
4. [The proof loop (sequence diagram)](#4-the-proof-loop-sequence-diagram)
5. [The event model — three tiers](#5-the-event-model--three-tiers)
6. [The deploy flow (sequence diagram)](#6-the-deploy-flow-sequence-diagram)
7. [The live-money conjunction (gate diagram)](#7-the-live-money-conjunction-gate-diagram)
8. [Trust boundaries at a glance](#8-trust-boundaries-at-a-glance)
9. [Repository layout → architecture mapping](#9-repository-layout--architecture-mapping)

---

## 1. The one-paragraph architecture

Veridex is one chain in which **no link trusts the previous one**:

```
AGENT proposes → LAW recomputes → POLICY gates → VENUE executes → PROOF verifies → LEADERBOARD ranks
```

TxLINE de-margined odds flow through **one normalizer** into frozen per-tick `MarketState`
snapshots. The **runtime** runs N agents concurrently on identical snapshots and seals the run —
ticks, decisions, errors, and (for live windows) the reconstructed closing line — into a
hash-covered **sealed prefix**. The **law** deterministically recomputes edge/CLV from those
sealed bytes, never from an agent's claims. The **policy gate** decides, in two phases around the
venue quote, whether acting is *safe*; the **venue adapter** executes behind multiple independent
locks, and its receipts are structurally non-scoring. The **proof layer** recomputes everything
fresh — seven falsifiable checks, a Merkle root-forest, a manifest hash anchored as a Solana
Memo — and exposes `POST /runs/{id}/verify` so anyone can re-derive the verdict. The
**leaderboard** ranks on recomputed CLV only. The **deploy platform** (Studio, API, CLI/SDK) pins
typed, bounded configs into durable `AgentInstance` records and launches every run through one
shared runner seam, so deployed agents earn the exact same proof as arena runs.

---

## 2. The big picture (system diagram)

```mermaid
flowchart TB
    %% ============ EXTERNAL SYSTEMS ============
    subgraph EXT["External systems"]
        TXLINE["TxLINE API<br/>(StablePrice odds: SSE stream,<br/>updates history, snapshots)"]
        GAMMA["Polymarket Gamma API<br/>(market discovery)"]
        CLOB["Polymarket CLOB<br/>(order book + orders, mainnet)"]
        SOLANA["Solana devnet<br/>(Memo anchor + TxLINE subscribe)"]
    end

    %% ============ INGEST ============
    subgraph INGEST["Ingest — veridex/ingest/"]
        AUTH["txline_auth.py<br/>guest JWT + on-chain subscribe<br/>+ token activate"]
        NORM["txline_normalize.py<br/>ONE normalizer →<br/>frozen MarketState"]
        LIVE["live_client.py<br/>SSE stream"]
        HIST["txline_client.py<br/>odds updates + CON-040<br/>closing reconstruction"]
        REC["recorder.py + replay_pack.py<br/>content-hashed ReplayPacks"]
        FEEDH["feed_health.py<br/>ops telemetry — NEVER proof"]
    end

    %% ============ AGENTS ============
    subgraph AGENTS["Agents — propose only"]
        STRAT["strategies/<br/>momentum v1 · Sharp Momentum v2<br/>value · baseline · LLM shell"]
        ACTION["AgentAction<br/>market_key + side +<br/>UNTRUSTED claimed_edge"]
        STRAT --> ACTION
    end

    %% ============ RUNTIME ============
    subgraph RUNTIME["Runtime — veridex/runtime/"]
        ORCH["orchestrator.py — CompetitionRun<br/>one run / N agents,<br/>identical inputs per tick"]
        LRUN["live_runner.py<br/>stream → seal close → finalize"]
        WIN["window.py — RunWindow<br/>end rules + sealed window config"]
        SEAL["evidence.py<br/>evidence_hash = sha256 over<br/>canonical SEALED PREFIX"]
    end

    %% ============ TRUST CORE ============
    subgraph TRUST["Deterministic trust core — import-audited, zero LLM SDK"]
        LAW["law/recompute.py<br/>CLV from sealed entry vs close;<br/>refuses suspended/missing closes"]
        EDGE["law/edge.py<br/>executable edge = p·price − 1"]
        SCORE["scoring.py + leaderboard.py<br/>is_scored · CLV-only rank key"]
        CHECKS["checks/build.py<br/>7 falsifiable proof checks"]
        VERIF["verifier/recompute.py<br/>verify_run: recompute FRESH<br/>from sealed bytes"]
    end

    %% ============ POLICY + EXECUTION ============
    subgraph EXEC["Policy + execution — downstream of the seal"]
        GATE["policy/gate.py<br/>two-phase gate<br/>(pre-quote / post-quote)"]
        BREAKER["policy/circuit_breaker.py<br/>pure CLOSED/OPEN/HALF_OPEN"]
        LANE["execution/runner.py<br/>execution lane — receipts<br/>NON-SCORING (evidence=False)"]
        RESOLVER["venues/polymarket_resolver.py<br/>fail-closed market→token<br/>(MarketUnavailable, never guess)"]
        ADAPTER["venues/polymarket.py<br/>decimal-odds quotes;<br/>write path triple-locked"]
        FAKE["venues/sx_bet.py<br/>FakeVenueAdapter (dry-run)"]
        PREFL["venues/polymarket_preflight.py<br/>tri-state checks → live_ready"]
    end

    %% ============ CHAIN ============
    subgraph CHAIN["Chain — veridex/chain/"]
        MERKLE["merkle.py<br/>per-domain root-forest"]
        ANCHOR["anchor.py<br/>manifest hash → Memo tx"]
    end

    %% ============ PLATFORM ============
    subgraph PLATFORM["Deploy platform + API"]
        SEAM["veridex_agent/run.py<br/>standalone_run —<br/>THE single runner seam"]
        DEPLOY["api/deploy.py + deploy/<br/>preflight → pin AgentInstance →<br/>async launch"]
        ROUTER["api/router.py<br/>/runs/:id/verify · /leaderboard ·<br/>/demo/run · WS event stream"]
        STORE["store.py<br/>Postgres / in-memory:<br/>runs · events · instances"]
        BT["backtest/<br/>runner + honest report"]
    end

    %% ============ FRONTEND ============
    subgraph FE["Frontend — apps/web"]
        UI["Studio · Cockpit · Inspector<br/>Proof Card · Leaderboard · H2H"]
        WSC["lib/ws.ts — canonical events,<br/>gap ⇒ resync"]
        EGATE["lib/edge-gate.ts — edge renders<br/>ONLY behind a real venue quote"]
    end

    %% ============ FLOWS ============
    TXLINE --> AUTH
    AUTH --> LIVE
    TXLINE --> LIVE
    TXLINE --> HIST
    LIVE --> NORM
    HIST --> NORM
    REC --> NORM
    LIVE -.captured.-> REC
    NORM -->|"MarketState ticks"| ORCH
    LRUN --> ORCH
    HIST -->|"CON-040 close"| LRUN
    WIN --> LRUN
    AGENTS -->|"decide(snapshot)"| ORCH
    ORCH --> SEAL
    SEAL -->|"sealed prefix"| LAW
    LAW --> SCORE
    SCORE --> CHECKS
    SEAL --> CHECKS
    CHECKS --> VERIF
    SCORE --> MERKLE
    SEAL --> MERKLE
    MERKLE -->|"manifest hash"| ANCHOR
    ANCHOR --> SOLANA
    SOLANA -.subscribe tx.-> AUTH

    %% execution lane (downstream of seal)
    SEAL -->|"sealed score_rows only"| LANE
    LANE --> GATE
    BREAKER --> GATE
    EDGE --> GATE
    GATE -->|approved| ADAPTER
    GATE -->|approved dry_run| FAKE
    RESOLVER --> ADAPTER
    PREFL -.live_ready.-> ADAPTER
    GAMMA --> RESOLVER
    ADAPTER <--> CLOB
    LANE -."receipts (evidence=False)<br/>NEVER back into scoring".- SEAL

    %% platform
    SEAM --> ORCH
    SEAM --> LRUN
    SEAM --> LANE
    DEPLOY --> SEAM
    BT --> ORCH
    STORE <--> ROUTER
    SEAM --> STORE
    VERIF --> ROUTER
    FEEDH -.ops only.-> ROUTER

    %% frontend
    ROUTER --> UI
    ROUTER --> WSC
    WSC --> UI
    EGATE --> UI
```

Reading keys for the diagram:

- **Solid arrows** are data/control flow. **Dotted arrows** are deliberately weak couplings —
  ops-only telemetry, or the receipts edge, which exists only to say it *does not* flow back.
- Everything inside **"Deterministic trust core"** is statically import-audited to contain zero
  LLM SDK code (`veridex/verifier/import_audit.py`), and the audit itself runs as the live
  `llm_boundary` proof check.
- The **execution lane hangs off the sealed run**, not off the agents: it consumes sealed
  `score_rows` only, and nothing it produces can alter a score, a hash, or a rank
  (proven byte-for-byte in `tests/test_standalone_run.py:220` and
  `tests/test_execution_integration.py:165`).
- The **single runner seam** (`veridex_agent/run.py::standalone_run`) is the only path from "a
  configured agent" to "a sealed, verified run" — the deploy endpoint, the CLI, and the SDK all
  route through it. No parallel runner exists.

---

## 3. Layer responsibilities

| Layer | Modules | Owns | Must never |
|---|---|---|---|
| **Ingest** | `veridex/ingest/` | Auth, SSE stream, odds history, the ONE normalizer, ReplayPacks, CON-040 closing reconstruction, feed health | Let telemetry (feed health) become evidence; parse live and replay through different code paths |
| **Agents** | `veridex/strategies/`, `veridex/runtime/agent.py` | Proposing constrained `AgentAction`s; strategy state over past ticks only | Score themselves; see a future tick; differ in inputs from a co-competing agent |
| **Runtime** | `veridex/runtime/` | The incremental run core (feed/finalize), live windows, the sealed prefix, evidence hashing, persist-then-broadcast | Let concurrency reach the deterministic seal; feed a tick after finalize; fabricate a closing line |
| **Trust core** | `veridex/law/`, `scoring.py`, `leaderboard.py`, `checks/`, `verifier/`, `policy/`, `ingest/` | Recomputing every number from sealed evidence; the 7 checks; CLV-only ranking | Import an LLM SDK; trust a claimed edge; hardcode a PASS; rank on anything but CLV |
| **Policy + execution** | `veridex/policy/`, `veridex/execution/`, `veridex/venues/` | Two-phase gating, the breaker, sizing, honest receipts, fail-closed venue resolution, the operator-only live path | Mint a second gating authority; fabricate a fill; guess a token; place real money without every lock open |
| **Chain** | `veridex/chain/` | Merkle root-forest, the manifest, the Memo anchor | Anchor anything other than the manifest hash; claim an anchor that didn't happen |
| **Platform** | `veridex/api/`, `veridex/deploy/`, `veridex/backtest/`, `veridex/competition/`, `veridex/store.py`, `veridex_agent/` | The deploy loop, durable AgentInstances, the verify/read API, the canonical event log, backtests, the single runner seam | Launch without a persisted instance; pass live-money deps over HTTP; leak a raw trace into a record or response |
| **Frontend** | `apps/web/` | Rendering served truth: contracts-first adapters, the edge display gate, the untrusted-LLM fence, honest mode labels | Reimplement law/scoring/checks; compute a client-side "pinned" hash; render an edge without a real venue quote |

---

## 4. The proof loop (sequence diagram)

One windowed live run, end to end — the flow a judge's Verify click retraces:

```mermaid
sequenceDiagram
    autonumber
    participant TX as TxLINE
    participant LR as live_runner
    participant CR as CompetitionRun
    participant AG as Agents (xN)
    participant LAW as law/recompute
    participant ST as Store
    participant CH as checks + verifier
    participant SOL as Solana devnet
    participant J as Anyone (judge)

    TX->>LR: SSE tick (StablePrice)
    LR->>LR: normalize → MarketState<br/>filter fixture + market allowlist
    LR->>CR: feed(snapshot)
    CR->>AG: decide(snapshot) — concurrent,<br/>timeout-wrapped, fail-closed
    AG-->>CR: AgentAction (claimed edge = untrusted)
    CR->>CR: append tick + decision/error<br/>RunEvents (sealed prefix)
    Note over CR,ST: each event: persist FIRST,<br/>then broadcast to spectators

    TX-->>LR: first in-running tick (kickoff)
    LR->>TX: GET /odds/updates/{fid}
    TX-->>LR: full movement history
    LR->>LR: reconstruct CON-040 close<br/>(last pre-InRunning, per market)
    alt complete authoritative close
        LR->>CR: feed_closing(close) — sealed, no decisions
    else fetch failed / no close / incomplete coverage
        LR->>LR: DEGRADE to window-CLV<br/>+ non-sealed ops marker (never fabricate)
    end

    LR->>CR: finalize(window)
    CR->>CR: seal window_config into prefix<br/>validate events → evidence_hash
    CR->>LAW: recompute(entry, action, closing) per decision
    LAW-->>CR: clv_bps | "pending" | invalid(reason)
    CR->>ST: persist sealed RunResult (run_id)

    LR->>CH: score_run → manifest → root-forest<br/>→ manifest_hash → 7 checks → proof card
    LR->>SOL: Memo tx (data = manifest hash)
    SOL-->>LR: signature (~1.3 s confirm)

    J->>CH: POST /runs/{id}/verify
    CH->>ST: load sealed run
    CH->>CH: recompute evidence_hash + scores +<br/>manifest FRESH — rebuild all 7 checks
    CH-->>J: verified + per-check verdict<br/>(⚠ NOT fully verified if any blocking check fails)
```

Two properties make this loop trustworthy rather than decorative:

1. **The proof is always downstream of the seal** — scores, checks, manifest, and anchor are
   computed only after `finalize`, from sealed bytes (`veridex/runtime/live_runner.py:417-451`).
2. **Verify recomputes; it never echoes.** Step 20 re-derives the score rows and the manifest from
   the sealed prefix, so a doctored persisted score is caught even when the seal is intact
   (`veridex/verifier/recompute.py:171-233`; `metrics_recomputed` in
   `veridex/checks/build.py:182-318`).

---

## 5. The event model — three tiers

```mermaid
flowchart LR
    subgraph SEALED["Tier 1 — SEALED PREFIX (evidence_hash covers exactly this)"]
        direction TB
        T1["tick RunEvents<br/>(full MarketState snapshots)"]
        T2["decision / error RunEvents<br/>(the sealed agent actions)"]
        T3["window_config RunEvent<br/>(end rule + horizon + end ts)"]
    end

    subgraph DERIVED["Tier 2 — DERIVED TAIL (evidence=False, derived_from refs)"]
        direction TB
        D1["LAW_RESULT · SCORE_UPDATE"]
        D2["POLICY_RESULT · EXECUTION_SUBMITTED ·<br/>EXECUTION_RECEIPT · APPROVAL_AUDIT"]
        D3["PROOF_ANCHOR · EXECUTION_ROUTE ·<br/>COMPETITION_FINALIZED"]
    end

    subgraph OPS["Tier 3 — OPS TELEMETRY (structurally unsealable)"]
        direction TB
        O1["RuntimeEvent: model calls,<br/>latency, tokens, status"]
        O2["feed health · degrade markers ·<br/>stream-interrupt causes"]
    end

    SEALED -->|"projected 1:1, hash-bound<br/>(payload_hash over the FULL sealed event)"| DERIVED
    SEALED -. "recompute inputs" .-> D1
    OPS -. "no sequence_no, no evidence flag,<br/>no payload_hash ⇒ CANNOT enter" .-> SEALED
```

- Tier 1 is what `evidence_integrity` protects; change one byte and verification fails.
- Tier 2 is recomputable *from* Tier 1 — which is why tampering it is caught by
  `metrics_recomputed` rather than needing to be hashed itself. Receipts live here and can never
  become skill evidence (`receipt_separation`).
- Tier 3 is made unsealable by *shape*: a `RuntimeEvent` lacks the three fields the evidence path
  requires (`veridex/runtime/runtime_events.py:1-13`), so no bug or config can promote telemetry
  into proof.
- The live spectator stream is a verified projection of Tier 1 + 2: finalize asserts the
  live-persisted prefix is byte-equivalent to the offline projection before appending the tail
  (`veridex/competition/service.py:417-427`).

---

## 6. The deploy flow (sequence diagram)

`configure → preflight → deploy → observe → verify`, as actually wired:

```mermaid
sequenceDiagram
    autonumber
    participant U as User (Studio / API / CLI)
    participant EP as POST /agents/deploy
    participant PF as deploy preflight
    participant ST as Store (durable)
    participant BG as background task
    participant SR as standalone_run (single seam)
    participant V as POST /runs/:id/verify

    U->>EP: DeployConfig (typed at the wire — bad types ⇒ 422)
    EP->>PF: named checks: config (bounds +<br/>lookback ≥ min_movements) · feed_health ·<br/>market_mapped · policy_limits
    alt any check fails
        PF-->>U: 422 naming every failing check<br/>NO instance row, NO run
    else all pass
        EP->>EP: pin config_hash + policy_hash<br/>mint run_id (before any launch)
        EP->>ST: PERSIST AgentInstance (PENDING)<br/>+ the preflight audit that gated it
        EP->>BG: create tracked asyncio task
        EP-->>U: {instance_id, config_hash,<br/>policy_hash, run_id} — returns BEFORE seal
        BG->>ST: status → RUNNING
        BG->>SR: run (replay ticks OR live window)<br/>+ optional execution lane (non-paper)
        SR->>ST: persist sealed run under the pre-known run_id
        alt clean seal
            BG->>ST: status → SEALED
        else pre-seal failure
            BG->>ST: status → FAILED +<br/>controlled reason enum (no raw trace)
        end
        U->>V: verify the deployed run
        V-->>U: same recompute, same proof shape<br/>as any arena run (one flow to proof)
    end
```

Design points visible in the diagram: **persist-then-launch** (a refused deploy leaves zero rows;
a crashed process leaves a durable record), **return-before-seal** (the response never blocks on a
multi-hour window), the **controlled failure vocabulary** (raw tracebacks go to logs only), and
**one flow to proof** (deployed runs verify through the identical endpoint as arena runs).

---

## 7. The live-money conjunction (gate diagram)

Every clause below must hold for one real order. Missing any clause does not error — it degrades
to a dry simulation that records *why*.

```mermaid
flowchart TB
    START(["execution requested"]) --> MODE{"execution_mode ==<br/>LIVE_GUARDED?<br/>(structural FIRST conjunct)"}
    MODE -- "no — paper / dry_run /<br/>any future mode" --> DRY["dry-run simulation<br/>(FakeVenueAdapter)"]
    MODE -- yes --> DEPS{"operator LiveExecutionDeps<br/>supplied? (never via HTTP)"}
    DEPS -- no --> DEG1["degrade → dry<br/>reason: missing_live_deps"]
    DEPS -- yes --> READY{"live_ready is True?<br/>(requires operator-confirmed<br/>neg-risk approval AND 1-share FAK smoke)"}
    READY -- no --> DEG2["degrade → dry<br/>reason: live_ready_false"]
    READY -- yes --> REAL{"adapter declares<br/>PROVIDES_REAL_VENUE_QUOTE?"}
    REAL -- no --> DEG3["degrade → dry<br/>reason: non_real_adapter"]
    REAL -- yes --> ARMED["ROUTE ARMED<br/>(breaker cell constructed)"]

    ARMED --> PRE{"pre-quote gate:<br/>kill switch · breaker OPEN? ·<br/>live stake cap · allowlists ·<br/>order cap · eligibility"}
    PRE -- deny --> NOPE1["DENIED — zero venue I/O"]
    PRE -- pass --> QUOTE["depth-aware quote<br/>priced for_size = stake<br/>(the SAME size that submits)"]
    QUOTE --> POST{"post-quote gate:<br/>staleness · liquidity ·<br/>real slippage · executable edge"}
    POST -- deny --> NOPE2["DENIED"]
    POST -- pass --> LOCK2{"adapter's OWN lock:<br/>write_enabled AND dry_run=False<br/>AND write client injected<br/>(does NOT trust the route)"}
    LOCK2 -- fail --> REFUSE["PolymarketWriteDisabled —<br/>refused before the wire"]
    LOCK2 -- pass --> RESOLVE{"resolver: exact market + token<br/>(draw ⇒ YES on draw-binary;<br/>away ⇒ away-WINS token)"}
    RESOLVE -- "any ambiguity" --> MU["MarketUnavailable —<br/>never guess a token"]
    RESOLVE -- resolved --> SUBMIT["FAK order — native tick-rounded<br/>price on the wire, (0,1) guarded"]
    SUBMIT --> POLL["poll until terminal;<br/>timeout ⇒ honest UNRESOLVED<br/>(counts as executed failure → breaker)"]

    DEG1 & DEG2 & DEG3 --> ROUTEEVT["EXECUTION_ROUTE event:<br/>degraded_because_not_armed + reason<br/>(evidence=False — auditable, never sealed)"]

    style DRY fill:#e8f0e8,stroke:#4a4
    style DEG1 fill:#fdf3d8,stroke:#b90
    style DEG2 fill:#fdf3d8,stroke:#b90
    style DEG3 fill:#fdf3d8,stroke:#b90
    style SUBMIT fill:#fbe4e4,stroke:#c33
    style REFUSE fill:#e8f0e8,stroke:#4a4
    style MU fill:#e8f0e8,stroke:#4a4
```

The two shaded families tell the safety story: green boxes are safe terminal states you reach *by
default* (the safe state is the state you get by doing nothing); the single red box — a real
submit — is reachable only through every gate in sequence, and to date **it has never been
exercised with real funds** (the first 1-share smoke is a human decision in the
[operator runbook](operator-runbook.md)). Note also that the route gates and the adapter lock are
**independent**: the money gate does not trust the routing layer, so a bug in one lock still
leaves the other closed.

---

## 8. Trust boundaries at a glance

```mermaid
flowchart LR
    subgraph UNTRUSTED["UNTRUSTED"]
        LLM["LLM / strategy claims<br/>(reason, confidence, claimed_edge)"]
        VENUE_IN["Venue responses<br/>(labels, statuses)"]
    end

    subgraph RECORDED["RECORDED, THEN VERIFIED"]
        ACTIONS["Sealed agent actions"]
        TICKS["Sealed market snapshots"]
    end

    subgraph AUTHORITATIVE["AUTHORITATIVE (recomputed)"]
        LAWX["Law recompute (CLV, validity)"]
        CHECKSX["7 proof checks"]
        RANK["CLV-only leaderboard"]
    end

    LLM -- "recorded as metadata,<br/>fenced NOT AN INPUT TO SCORE" --> ACTIONS
    ACTIONS --> LAWX
    TICKS --> LAWX
    LAWX --> RANK
    ACTIONS --> CHECKSX
    TICKS --> CHECKSX
    VENUE_IN -- "reconciled from matched-size<br/>numbers; receipts non-scoring" --> CHECKSX
```

The full invariant registry — thirteen rules, each with the test, audit, or CHECK constraint that
enforces it — is [deep-dive §14](technical-deep-dive.md#14-the-trust-boundary-registry). The five
most load-bearing:

1. **Checks ≠ metrics** — CLV is never a check; checks certify the record, metrics rank
   performance.
2. **No hardcoded PASS** — every check recomputes from sealed evidence and fails closed.
3. **Receipts non-scoring** — a fill can never become proof; provably causally inert.
4. **CLV-only ranking** — confidence, Kelly, and proof-completeness never enter a rank key.
5. **Zero LLM SDK in the trust path** — statically audited, fail-closed if a trust directory is
   missing.

---

## 9. Repository layout → architecture mapping

```
veridex-arena/
├── veridex/                  # Python backend — the proof engine
│   ├── ingest/               #   §2 diagram: Ingest (auth, normalizer, packs, feed health)
│   ├── runtime/              #   Runtime (CompetitionRun, live runner, window, evidence seal)
│   ├── law/                  #   Trust core: the deterministic law + executable edge
│   ├── checks/               #   Trust core: the 7-check taxonomy
│   ├── verifier/             #   Trust core: verify_run + proof cards + the import audit
│   ├── scoring.py            #   Trust core: is_scored + the metric stack
│   ├── leaderboard.py        #   Trust core: cross-run CLV-only ranking
│   ├── policy/               #   Two-phase gate · envelope · pure circuit breaker
│   ├── execution/            #   The execution lane (receipts non-scoring) + edge legibility
│   ├── venues/               #   VenueAdapter seam · Polymarket read/write · resolver · preflight
│   │   └── _vendor/          #   Vendored, pinned MIT-licensed Polymarket CLOB client
│   ├── strategies/           #   Agents: momentum v1 · Sharp Momentum v2 · value · sharp stats
│   ├── backtest/             #   BacktestRunner + honest-mode reports
│   ├── deploy/               #   Deploy preflight · durable AgentInstance
│   ├── competition/          #   Arena service · canonical event log · operator live routing
│   ├── chain/                #   Merkle root-forest + Solana Memo anchor
│   ├── api/                  #   FastAPI: verify · deploy · leaderboard · WS · demo
│   └── store.py              #   Postgres / in-memory persistence
├── veridex_agent/            # The SDK/CLI — routes through the SAME single runner seam
├── apps/web/                 # Next.js frontend (Studio · Cockpit · Inspector · Proof Card)
├── scripts/                  # demo_phase2d.py (judge demo) · operator smoke · live tooling
├── tests/                    # 1,000+ offline tests incl. the byte-for-byte golden seal suite
└── docs/                     # this file · technical-deep-dive.md · runbooks · FAQ
```

For the depth behind any box in these diagrams — the design decision, its rejected alternative,
the enforcing test, and the file:line — go to the
[technical deep-dive](technical-deep-dive.md): §2 data layer, §3 runtime, §4 law/scoring, §5
proof, §6 policy/execution, §7 price-unit honesty, §8 venues, §9 the live-money conjunction, §10
strategies, §11 deploy, §12 the real-data experiment, §16 the design-decision ledger.
