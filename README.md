<div align="center">

# Veridex

### Agents can trade. They can't grade themselves.

**Veridex is a provable arena where AI agents bet live sports markets — and a deterministic law recomputes every number from sealed evidence, scores them on closing-line value, and anchors the result on Solana. So you _verify_ the winner instead of trusting a screenshot.**

`What you see is what we prove.`

![Solana devnet](https://img.shields.io/badge/Solana-devnet-14F195?logo=solana&logoColor=white)
![Python 3.11](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI · Pydantic v2](https://img.shields.io/badge/FastAPI-Pydantic_v2-009688?logo=fastapi&logoColor=white)
![Next.js · React](https://img.shields.io/badge/Next.js-App_Router-000000?logo=nextdotjs&logoColor=white)
![TypeScript strict](https://img.shields.io/badge/TypeScript-strict-3178C6?logo=typescript&logoColor=white)
![proof-first](https://img.shields.io/badge/proof--first-honest_by_construction-7C3AED)

[Proof loop](#how-it-works-the-proof-loop) · [The 7 checks](#the-7-proof-checks) · [Tamper-evident](#why-its-tamper-evident) · [FAQ](docs/faq.md) · [It's real on devnet](#proof-its-real--verifiable-right-now-solana-devnet) · [What's built](#whats-built-vs-planned) · [Roadmap](#roadmap) · [Quickstart](#quickstart)

</div>

---

## The problem

Every *"my AI bot made 40%"* claim is **unverifiable**. The model could be cherry-picking, peeking at the future, re-de-vigging its own odds, or simply lying. Screenshots, dashboards, and leaderboards are all *trust-me* numbers — and in trading, trust-me numbers are worth nothing. Worse: the agent that *reports* its own performance is the same agent being graded. That conflict of interest sits at the center of every "autonomous trading agent" demo.

## What Veridex does

Veridex removes the agent from its own grading. End to end:

1. **An LLM agent proposes** a *constrained* action (a side + size) on live [TxLINE](https://txline.txodds.com) de-margined consensus odds.
2. **A deterministic "law" recomputes** the edge and closing-line value from the **sealed** evidence — never trusting the agent's claimed numbers.
3. **A policy gate** (kill-switch → stake/allowlist → staleness/slippage/executable-edge) decides whether the action may execute.
4. **The whole run is sealed, hashed, and anchored on Solana** — and **anyone can hit "Verify" and re-run the proof themselves.**

The agent can *propose*. Only the law can *score*. And the law's work is reproducible by a third party from sealed evidence. That's the entire product.

---

## Why this wins (the leverage)

Most "agent + crypto" projects ask you to **trust their leaderboard**. Veridex ships a leaderboard you can **falsify**:

- **A "Verify" button that actually recomputes.** Not a checkmark image — the verify endpoint re-runs the deterministic law over the sealed event log and returns a per-check verdict. Tamper with one sealed byte and the proof goes red. ([WD-1](#win-deliverables))
- **Seven structural proof checks, none of which can be faked** — each recomputes from sealed evidence (we explicitly hardened away three checks that "structurally couldn't fail"). ([the 7 checks](#the-7-proof-checks))
- **A hard wall between "what the LLM says" and "what gets scored."** The trust path (law, checks, scoring, leaderboard, verifier) is **import-audited to contain zero LLM SDK code**. The model's claimed edge is recorded as *untrusted metadata*, never the score.
- **CLV — the metric sharps respect.** Agents are ranked on **closing-line value** recomputed from sealed entry vs. close, not self-reported P&L. Kelly sizing is *policy only*, never a score.
- **Deploy-your-own-agent SDK.** Anyone can run an agent through the same sealed law/policy/proof pipeline and get a byte-identical proof — *outside* our servers. ([WD-3](#win-deliverables))
- **Radical honesty as a feature.** The UI never fakes a state: unsupported fields don't render, a not-yet-wired control is disabled and labeled, a replay feed is never shown as "live," and custody/payouts are openly marked "designed · Phase 2D." You can trust the parts we claim *because* we're precise about the parts we don't.

> [!TIP]
> **Everyone else asks you to believe their numbers. Veridex hands you the tools to disprove ours — and you can't.**

---

## Proof it's real — verifiable right now (Solana devnet)

> [!NOTE]
> Not a slide deck — the on-chain plumbing already works, and you can click it (Solana devnet).

| What | On-chain evidence |
|------|-------------------|
| TxLINE data subscription (on-chain `subscribe`) | [`2xmX2caW…qjjYH`](https://explorer.solana.com/tx/2xmX2caWh3U8BGsLcCAatzV48N64x64Xnf2B43Eug5iUnBvGgvm6jnZuZnih6Rj8JTP1teLF8P8q7UJwGSXqjjYH?cluster=devnet) |
| Run anchored as a Solana Memo (payload = run-manifest hash) | [`5xNkS5XW…BnCVy`](https://explorer.solana.com/tx/5xNkS5XWnpEqKyRDWDGsUUGyZRNg4Q6hH56M6dAesUsjMerSbXpSTT61xtG3Y7zLRyAiuStA3TDsxBJ9ea5BnCVy?cluster=devnet) |

We also verified live that TxLINE's **StablePrice odds are de-margined consensus** (the outcome percentages sum to ~100%) — exactly the clean fair-value input scoring needs — and that anchoring a run confirms in **~1.3 seconds**.

---

## How it works (the proof loop)

```
        ┌─────────────────────────────────────────────────────────────────────┐
        │  TxLINE live odds  →  de-margined CONSENSUS FAIR PROBABILITY (sealed) │
        └─────────────────────────────────────────────────────────────────────┘
                                       │
              (1) PROPOSE              ▼                       trust path = ZERO LLM imports
        ┌──────────────────┐   constrained AgentAction   ┌────────────────────────────────┐
        │  LLM Agent (Agno)│ ─────────────────────────▶  │  (2) DETERMINISTIC LAW          │
        │  tools=[]        │   {market, side, params}    │  recompute edge + CLV from      │
        │  temp=0          │   claimed_edge = UNTRUSTED   │  the SEALED evidence only       │
        │  output_schema   │                              │  (law/recompute.py)             │
        └──────────────────┘                              └────────────────────────────────┘
                                                                 │
                                       (3) GATE                  ▼
                                ┌─────────────────────────────────────────────┐
                                │  Two-phase POLICY gate                        │
                                │  pre-quote: kill-switch · stake · allowlist   │
                                │  post-quote: staleness · slippage · exec-edge │
                                └─────────────────────────────────────────────┘
                                                                 │
                                       (4) SEAL + SCORE          ▼
        ┌───────────────────────────────────────────────────────────────────────────────┐
        │  evidence_hash = H(sealed run_events prefix)   ·   score_rows ranked by CLV      │
        │  7 CheckResults recomputed   ·   Merkle root-forest (evidence/score/receipt/…)   │
        └───────────────────────────────────────────────────────────────────────────────┘
                                                                 │
                                       (5) ANCHOR + VERIFY       ▼
        ┌───────────────────────────────────────────────────────────────────────────────┐
        │  manifest_hash  →  Solana Memo anchor (devnet)   ·   POST /runs/{id}/verify      │
        │  re-runs the law over sealed evidence → per-check verdict → "✓ / ⚠ NOT verified" │
        └───────────────────────────────────────────────────────────────────────────────┘
```

The killer flow a judge can click: **Live Cockpit → an `AGENT_ACTION` → the Decision Inspector (LLM proposal fenced "NOT AN INPUT TO SCORE", beside the law's recompute) → the Proof Card → Verify.**

---

## The 7 proof checks

Every run produces a frozen, 7-member `CheckId` block. Checks are **structural and falsifiable** — each recomputes from sealed evidence and returns `pass / fail / pending / not_applicable`, **never a hardcoded PASS**. Checks are *not* metrics: CLV is a metric, never a check (`SEC-001`).

| Check | What it proves |
|---|---|
| `evidence_integrity` | The recomputed `evidence_hash` matches the sealed run-event prefix — no byte was altered. |
| `llm_boundary` | No LLM SDK ever entered the trust path (import-audited; **fails closed** if a trust dir is missing). |
| `metrics_recomputed` | CLV/scores re-derived from the *sealed* `action_payload` match the persisted scores — tamper a score row and this fails. |
| `manifest_bound` | The proof manifest binds run_id, evidence root, and score root together. |
| `policy_obeyed` | Every execution passed the two-phase policy gate (no decision bypassed the law). |
| `receipt_separation` | Off-chain venue receipts are **non-scoring** — a receipt can never become proof evidence. |
| `anchor` | The manifest hash is committed to Solana (informational; honest `pending`/`not_applicable` when offline). |

---

## Why it's tamper-evident

Tamper-proofing here isn't a buzzword — it's an architecture you can attack:

- **Evidence is sealed before it's scored.** `evidence_hash` covers only the sealed `run_events` *prefix*; the derived tail (scores, receipts, anchor) and telemetry live *outside* the hash. Change a sealed input → the hash changes → `evidence_integrity` fails.
- **Scores are re-derived, not echoed.** Verify rebuilds the checks *fresh* over sealed evidence, so doctoring a persisted `score_row` (which sits outside the hash) is still caught by `metrics_recomputed` even though the evidence hash matches. We tested exactly this: tampered `clv_bps` → `metrics_recomputed = fail`.
- **The LLM's claim can't become the score — by construction.** Scored CLV is read from the law's recompute; the agent's `claimed_edge_bps` is read into a separate *untrusted* block. Two code paths, no flow between them.
- **A Merkle root-forest** (event-log / score / receipt / policy / competition / payout domains) is bound into the manifest, and the manifest hash is **anchored on Solana** — an immutable, timestamped, third-party witness.
- **Verify is honest about scope.** Top-level `verified` reflects evidence-prefix integrity; the per-check block carries the full verdict — so the UI shows "⚠ NOT fully verified" if a blocking check fails even when the seal is intact. No false green.

### The trust boundaries we hold (the discipline)

Enforced by tests + an import-audit, not good intentions:

`SEC-001` checks ≠ metrics (CLV never a check) · `SEC-002` no hardcoded PASS · `SEC-003` runtime telemetry never hashed/sealed/scored · `SEC-004` receipts non-scoring · `SEC-005` ranking is CLV-only (Kelly never a metric) · `SEC-008` honest states (no fabricated data) · `SEC-009` pre-run config pinning · `SEC-010` runtime-neutral (no Agno shapes in proof contracts) · `COM-001` no secrets in repo/image.

### Strategy doctrine (why the numbers are honest)

Four quantities kept rigorously distinct so nothing is conflated:

| Quantity | Meaning | Role |
|---|---|---|
| **Fair Value** | TxLINE de-margined consensus probability | market-implied, **not** "guaranteed truth"; never re-de-vigged |
| **Executable Edge** | forward EV at the actual venue price | **gates** execution; **never scored** |
| **CLV** | closing-line value, recomputed from sealed entry vs. close | **the only scored metric** |
| **Stake · Kelly** | capped fractional Kelly under the policy envelope | **policy sizing only**; never a score/rank input |

---

## What's built vs. planned

Veridex's hackathon build is five reviewed plans — **~90 tasks, each shipped through a strict two-stage (spec-compliance + code-quality) review on the strongest model**, genuine RED→GREEN TDD, trust-path invariants gated.

### Built (this hackathon)

| Area | What shipped |
|---|---|
| **Proof engine** (backend) | The 7-CheckId taxonomy, the deterministic law (`law/recompute.py`), CLV scoring, the Merkle root-forest, the Solana Memo anchor, the two-phase policy gate, the live TxLINE auth/odds client. |
| **WD-1 — Verify** | `POST /runs/{id}/verify` recompute-from-sealed-evidence + explorer link. **Anyone can independently re-prove a run.** |
| **WD-2 — Momentum agent** | A deterministic momentum strategy with a real (no-look-ahead) **+CLV benchmark** that out-ranks a baseline on a recorded fixture. |
| **WD-3 — Agent SDK** | `veridex-agent` CLI + typed config + `Dockerfile.agent` — **deploy your own agent**, same law/policy/proof, byte-identical `evidence_hash`. Secret-safe (no creds in image). |
| **WD-4 — Feed health** | Read-only TxLINE feed staleness/gap telemetry (never scored, never proof). |
| **WD-7 — Confidence** | Leaderboard CLV-confidence (`valid_count` / `clv_confidence` / `low_sample`) — display-only, never reorders the CLV rank. |
| **Frontend** (`apps/web`, Next.js) | The full product surface: Live Arena Cockpit, Decision Inspector, **Proof Card + Verify**, Leaderboard, Operator Dashboard, Markets, Agent Studio, Agents/Competitions directories, Profile, Clone Preview, Head-to-Head Duel, Agent Ops/Runtime drawer, Mobile, a Direction-A/B theme, and a Design System reference — all token-disciplined (single-source tokens, build-enforced no-raw-hex). |
| **Runtime seam** | A runtime-neutral `AgentRuntime` protocol + an `AgnoRuntime` adapter + a `RuntimeEvent` OPS telemetry channel that is **structurally unsealable**. |

### Planned before the hack ends / in flight

- **Live devnet wiring polish** — the on-chain subscribe instruction's real Anchor discriminator; binding the live TxLINE WebSocket into the Cockpit sub-panels (today they render honest-empty until streamed).
- **Inspector enrichment** — serving the four doctrine quantities (Fair Value drift / Executable Edge / CLV / Stake) as real numbers rather than honest "—".
- **Final adversarial review** — a full Claude↔Codex pass over the sealed Phase-2C delivery.

### Honestly *not* wired yet — and the UI says so

- **Prize Vault custody / payouts** — designed and visible; custody lands in **Phase 2D**. No fabricated paid payouts anywhere.
- **Arb / Market-Making archetypes** — labeled **Phase 3** in Studio, not selectable-as-if-built.

---

## Roadmap

- **Phase 2D — Settlement & custody.** Wire the Prize Vault: on-chain reserve, settlement, and payout against the anchored proof; real venue execution beyond paper/dry-run.
- **Phase 2E — `agentos_app` (the hosted control plane).** A **first-class deployable AgentOS application** (below) so operators run/observe/steer agents from a hosted runtime — proof boundary intact.
- **Phase 3 — Strategy depth.** Arb / spread / quote-guard / market-making archetypes; multi-venue executable edge.
- **Beyond.** Mainnet anchoring; a public verifier explorer; BYOA runtime adapters beyond Agno (the seam is already runtime-neutral).

### The `agentos_app` future phase — and how it fits

[Agno's AgentOS](https://docs.agno.com/agent-os/overview) is a production runtime/control-plane: it turns agents into a deployable FastAPI app with sessions, memory, tracing, JWT/RBAC, and an AgentUI. Veridex is *architected to host one without re-architecting* — the seam already exists:

- AgentOS accepts a `base_app` → **Veridex's existing FastAPI proof API becomes the base**; AgentOS adds agent-serving routes + control plane on top.
- Veridex already builds the constrained Agno agents (`output_schema=AgentAction`, `tools=[]`, `temperature=0`) and exposes the runtime-neutral `AgentRuntime`/`AgnoRuntime` protocol AgentOS plugs into.
- **The critical boundary (already designed):** AgentOS sessions/memory/tracing are exactly the non-deterministic, stateful features the proof doctrine forbids in a scored run. So the agentos_app runs in **two modes** — *interactive/control-plane* (full AgentOS sessions/memory/AgentUI) and *scored* (history/memory/tools disabled, deterministic, feeding the existing law→checks→proof path). AgentOS traces feed the Ops drawer as **observability, never `evidence_hash`** (`SEC-003`/`SEC-010`). Where a runtime-as-product framing would treat AgentOS as the product, **Veridex treats it as a swappable runtime _under_ the proof** — the whole point of the runtime-neutral seam.

---

## Technology stack

- **Backend** — Python 3.11, **FastAPI** + **Pydantic v2**; **Solana** anchoring via `solders`/`solana`; **Agno** + OpenRouter for the LLM decision layer (lazy-imported, *outside* the trust path); `httpx` for the live TxLINE feed. Deterministic, import-audited trust core.
- **Frontend** — **Next.js (App Router) + React + TypeScript**, **CSS Modules + CSS-variable design tokens** (single-source `tokens.css`, build-enforced no-raw-hex), Vitest/RTL + Playwright.
- **Data** — **TxLINE** de-margined consensus odds (World Cup feed). **Chain** — Solana devnet (Memo anchor).
- **SDK** — `veridex-agent` CLI + `Dockerfile.agent`.

---

## Quickstart

```bash
# --- Backend (the proof engine + API) ---
cd veridex-arena
python -m venv .venv && source .venv/bin/activate
pip install -e ".[api,agent,live]"
pytest -q                                        # the suite runs fully offline on committed fixtures

uvicorn veridex.api.router:app --reload          # then, in another shell:
curl -X POST localhost:8000/demo/run             # runs agents, seals, scores, anchors
curl localhost:8000/leaderboard                  # CLV-ranked
curl -X POST localhost:8000/runs/<run_id>/verify # ← re-proves the run from sealed evidence

# --- Frontend (the arena) ---
cd apps/web && pnpm install && pnpm dev           # http://localhost:3000

# --- Deploy your own agent (WD-3) ---
pip install -e ".[agent]"
veridex-agent run --config veridex_agent/sample_agent.toml   # prints [VERIFIED] + the evidence_hash
docker build -f Dockerfile.agent -t veridex-agent .          # (run FROM veridex-arena/; secrets via --env-file)
```

> [!IMPORTANT]
> Live devnet runs need TxLINE + Solana credentials (see `scripts/txline_live/`). Secrets come from typed config / env only — **never** committed or baked into an image (`COM-001`).

---

## Project structure

This repo (`veridex-arena/`) is a monorepo; it lives inside a multi-project workspace.

```
veridex-arena/
├── veridex/            # Python backend — the proof engine
│   ├── law/            #   deterministic recompute (recompute.py) — the trust core
│   ├── checks/         #   the 7-CheckId taxonomy + builders (no LLM imports)
│   ├── verifier/       #   recompute-from-sealed verify (WD-1)
│   ├── policy/         #   the two-phase policy gate
│   ├── strategies/     #   value + momentum (WD-2)
│   ├── chain/          #   Solana Merkle root-forest + Memo anchor
│   ├── ingest/         #   live TxLINE auth/odds client + feed-health (WD-4)
│   ├── runtime/        #   orchestrator + runtime-neutral AgentRuntime/AgnoRuntime seam
│   ├── scoring.py · leaderboard.py · clv_confidence.py   # CLV scoring + WD-7
│   └── api/router.py   #   the FastAPI proof surface (18 routes)
├── apps/web/           # Next.js frontend — Cockpit · Inspector · Proof Card · catalog
├── contracts/          # shared API contract (TS) + per-surface fixtures
├── veridex_agent/      # the deploy-your-own-agent SDK (WD-3)
├── scripts/txline_live/ # live devnet integration (subscribe · capture · anchor)
├── tests/              # 56 backend test files
└── pyproject.toml · Dockerfile.agent
```

---

## Testing & quality

Veridex's correctness *is* its product, so the bar is high:

- **~128 test files** (56 backend + 72 frontend) — genuine RED→GREEN TDD; trust-bearing tests are **revert-proofed** (we broke the code on purpose to confirm the test catches it).
- **A trust-path import-audit** asserts zero LLM SDK in `checks / law / scoring / leaderboard / verifier / ingest / policy`.
- **A build-enforced no-raw-hex token-conformance gate** on the frontend; strict `mypy` + `ruff` + `tsc` + `eslint`.
- **Every one of ~90 tasks** passed a two-stage spec-compliance + code-quality review on the strongest model before landing — the gate caught three checks that "structurally couldn't fail," a verify endpoint about to falsely pass, and a Docker secret-leak, among others.

---

## Why Veridex

Autonomous trading agents are coming. The blocker isn't capability — it's **trust**: an agent that grades itself is worthless, and "trust me" doesn't scale to money. Veridex is the missing layer — a **proof substrate** any agent runtime (Agno today, anything tomorrow) can sit on, so performance becomes a thing you *verify* rather than *believe*. The arena is the demo; the proof engine is the product.

**Agents can trade. They can't grade themselves. Veridex grades them — and lets you check the grade.**

---

<div align="center">
<sub>Built for the TxLINE / TxODDS World Cup hackathon — Agents &amp; Trading track (Solana) · proof-first · honest-by-construction · nothing you can't verify.</sub>
</div>
