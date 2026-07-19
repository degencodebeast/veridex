<div align="center">

# Veridex

### Agents can trade. They can't grade themselves.

**Veridex is the proof-and-deployment layer for autonomous sports-trading agents: configure an agent from a strategy template, deploy it into a live arena where agents compete head-to-head on identical sealed [TxLINE](https://txline.txodds.com) World Cup data, let it trade real venues under policy guardrails — then verify what actually happened from sealed evidence, on Solana, whether it found an edge or not.**

`What you see is what we prove.`

![Solana devnet](https://img.shields.io/badge/Solana-devnet-14F195?logo=solana&logoColor=white)
![Python 3.11](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI · Pydantic v2](https://img.shields.io/badge/FastAPI-Pydantic_v2-009688?logo=fastapi&logoColor=white)
![Next.js · React](https://img.shields.io/badge/Next.js-App_Router-000000?logo=nextdotjs&logoColor=white)
![TypeScript strict](https://img.shields.io/badge/TypeScript-strict-3178C6?logo=typescript&logoColor=white)
![proof-first](https://img.shields.io/badge/proof--first-honest_by_construction-7C3AED)

[What Veridex is](#what-veridex-is) · [Four pillars](#the-four-pillars) · [Real-data truth](#we-ran-it-on-real-world-cup-data-heres-the-truth) · [Proof loop](#how-it-works-the-proof-loop) · [The 7 checks](#the-7-proof-checks) · [Real money, fail-closed](#the-mode-ladder--how-real-money-stays-fail-closed) · [It's real on devnet](#proof-its-real--verifiable-right-now-solana-devnet) · [Quickstart](#quickstart) · [Judge demo](docs/deploy-judges.md) · [FAQ](docs/faq.md)

</div>

---

## The problem

Every *"my AI bot made 40%"* claim is **unverifiable**. The model could be cherry-picking, peeking at the future, re-de-vigging its own odds, or simply lying. Screenshots, dashboards, and leaderboards are *trust-me* numbers — and in trading, trust-me numbers are worth nothing. Worse: the agent that *reports* its own performance is the same agent being graded. That conflict of interest sits at the center of every "autonomous trading agent" demo.

## What Veridex is

**A provable autonomous sports-trading agent platform.** Users configure, deploy, observe, backtest, **compete**, and verify strategy agents that trade sports markets using TxLINE de-margined fair-value data, real venue quotes, deterministic recomputation, policy guardrails, and sealed proof cards.

The whole product is one chain, and no link trusts the previous one:

```
  AGENT proposes → LAW recomputes → POLICY gates → VENUE executes → PROOF verifies → LEADERBOARD ranks
```

- The **agent** can only *propose* a constrained action — its claimed edge is recorded as *untrusted metadata*, never scored.
- The **law** deterministically recomputes edge and closing-line value from **sealed evidence** — never from the agent's claims.
- The **policy gate** decides whether acting is *safe* (kill-switch, stake caps, staleness, slippage, executable edge, circuit breaker).
- The **venue adapter** executes (Polymarket) — and its receipts are **structurally non-scoring**: a fill can never become proof.
- The **verifier** re-runs the law from sealed bytes — anyone can, via `POST /runs/{id}/verify`.
- The **leaderboard** ranks on recomputed CLV only.

And it's an **arena**, not a single-bot demo: a competition runs **N agents concurrently on identical sealed inputs** — same ticks, same law, same policy — so rank differences are strategy, never luck of the feed. Deploy your own agent through the same pipeline and compete on the same falsifiable leaderboard.

Most agent demos ask you to trust the model's claim. Veridex makes the claim **auditable** — and it is honest enough to prove when an agent had **no** edge.

## The four pillars

| Pillar | What it is | Why it exists |
|---|---|---|
| **Agent Studio** | Configure and deploy trading agents from strategy templates — typed, bounded configs; a preflight that fails closed with named reasons; one click to a running, pinned agent. | Users need to *build* strategies, not watch hardcoded bots. |
| **Live Agent Arena** | A real-time room where agents **compete head-to-head on identical sealed inputs** — ingesting TxLINE data, producing actions, ranking on CLV, streaming their full decision trail (proposal → law → policy → receipt), with Head-to-Head Duel and a falsifiable leaderboard. | A single strategy result is easy to overread. Running agents head-to-head on one sealed tape shows which ideas survive the same evidence, which abstain, and which fail — a fair, replayable contest, not isolated backtest claims. |
| **Verification / Proof layer** | Deterministic recompute, 7 structural checks, evidence hashes, Merkle root-forest, Solana anchors, proof cards, and verify-yourself endpoints. | Agents can trade, but they can't grade themselves — so every result is recomputed from sealed evidence and independently checkable, letting the platform prove both wins and non-wins. |
| **Execution + Risk layer** | Policy envelopes, two-phase gating, quote freshness, slippage and stake caps, a circuit breaker, honest mode labels, and a fail-closed operator-only path to real money. | Production trading isn't just finding edge — it's deciding when acting is *safe*. |

### Agents are configurable instances, not fixed bots

```
AgentTemplate + AgentConfig + PolicyEnvelope + Evidence = AgentInstance
```

The **template** is the strategy family (`SharpMomentumAgent`, value-vs-venue, baseline…). The **config** is the real strategy — thresholds, market universe, warmup, quote requirements, sizing, risk caps, cooldowns, source/execution mode — all typed, bounded, and folded into a `config_hash`. The **policy envelope** is the execution boundary. Deploy pins all of it into a **durable, store-backed AgentInstance** (with its named preflight audit attached), so two users can deploy the same template with different configs, rank them head-to-head on the same falsifiable leaderboard, get different performance — and Veridex still proves exactly what each one did. Configs change *behavior*; **no config can change the trust rules** (law, checks, evidence integrity, receipt separation, scoring immutability).

---

## We ran it on real World Cup data. Here's the truth.

We pulled real TxLINE odds history across **18 finished World Cup fixtures**, sealed each into a content-hashed ReplayPack, and ran two experiments end-to-end through the pipeline. Both are recorded; here is what they say.

**Run-001 — a candidate CLV signal (our strongest result).** The `CumulativeDriftAgent` — the drift template built for smooth multi-day repricing — averaged **+61.19 bps CLV** across the 18-fixture filtered universe, **beating all three acting deterministic baselines** on this sample (favorite +4.6, threshold-move −126.5, seeded-random −341.7 bps), net-positive on 10 of 18 fixtures. Crucially this is a **candidate rung-1 CLV signal, not proven executable alpha**: CLV is measured against the sharp TxLINE close with no venue leg, and the effective sample is ~18 fixtures, so it is *directional evidence, not a statistical proof*. We then took the honest next step and tested it **out-of-sample (OOS)** — and it did not reproduce: the surviving OU-totals sub-signal *falsified* on its own metric and came out **NULL on independent settled outcomes** at only **N=2 effective fixtures**, so we keep it as a benchmark rather than promote a false edge. But it is the honest kind of positive — the agent beat its baselines on identical sealed inputs, and every number was **recomputed by the law, never self-reported**.

**Run-002 — the trust moat.** We then tested whether that candidate translated into **Polymarket venue edge** — and the platform stopped us from overclaiming. The rung-2 venue lane **works**: it priced **94.7% of drift's in-scope 1X2 decisions** against time-aligned Polymarket mids under a bounded-staleness bound, with a self-verified pinned coverage hash and `venue_source_id`. But the rung-2 *estimated mids* result was a near-perfect **monotonic longshot ramp** — **+607 bps** on 0–20% longshots decaying to **+33 bps** on 80–100% favorites — the fingerprint of a favorite-longshot / de-margin-scale divergence, **not** strategy-specific dislocation. So **Run-002 did NOT demonstrate executable venue edge.** These are estimated mids, not fills; `real_executable_edge_bps = None`; no profit and no fillability claim.

> [!IMPORTANT]
> **That refusal is the product.** It would have been trivial to headline "+607 bps." Instead the law priced the decisions, the `VenueBehaviorReport` slices surfaced the ramp, and Veridex called it what it is — a structural divergence to *falsify*, not alpha to trade. Most agent demos ask you to trust the bot; a trustworthy **"no executable edge yet"** is exactly what a proof layer is for. (Full run-notes live in the research record: `.omc/research/run-001-run-note.md` and `run-002-run-note.md`.)

---

## The trust moat

Most "agent + crypto" projects ask you to **trust their leaderboard**. Veridex ships a leaderboard you can **falsify**:

- **A "Verify" button that actually recomputes.** The verify endpoint re-runs the deterministic law over the sealed event log and returns a per-check verdict. Tamper with one sealed byte and the proof goes red. Deployed agents verify through the **same** endpoint as arena runs — one flow to proof.
- **Seven structural proof checks, none of which can be faked** — each recomputes from sealed evidence ([the 7 checks](#the-7-proof-checks)).
- **A hard wall between "what the LLM says" and "what gets scored."** The trust path is **import-audited to contain zero LLM SDK code**. The model's claimed edge is untrusted metadata, fenced in the UI as `NOT AN INPUT TO SCORE`.
- **CLV — the metric sharps respect** — recomputed from sealed entry vs. close; never self-reported P&L. Confidence keys off *scored picks*, so a thousand abstentions can never dress up as a "high-confidence" record.
- **Fair competition by construction.** A competition runs its agents concurrently over *identical sealed inputs* — no agent sees a different feed, a later tick, or a friendlier close. Rank differences are strategy. That's what makes the leaderboard (and the Head-to-Head Duel) a fair fight — and worth trusting.
- **A real venue, really integrated.** Polymarket quotes/execution with honest price-unit discipline (the native price is audit-only; the platform speaks decimal odds), an event→market resolver that **fails closed rather than guess a token**, and a display gate that renders an edge number **only when a genuine venue quote backs it**.
- **Deploy-your-own-agent, same proof.** The Studio deploy loop and the standalone SDK run *your* config through the same sealed law/policy/proof pipeline — same evidence discipline, pinned `config_hash`/`policy_hash`.
- **Radical honesty as a feature.** Five explicit run modes, never conflated; unsupported fields don't render; a degraded run records *why* it degraded; synthetic data self-labels. You can trust the parts we claim *because* we're precise about the parts we don't. Run-002 above is this feature in action: the platform refused to promote a longshot-ramp artifact into an edge claim.

---

## Proof it's real — verifiable right now (Solana devnet)

| What | On-chain evidence |
|------|-------------------|
| TxLINE data subscription (on-chain `subscribe`) | [`2xmX2caW…qjjYH`](https://explorer.solana.com/tx/2xmX2caWh3U8BGsLcCAatzV48N64x64Xnf2B43Eug5iUnBvGgvm6jnZuZnih6Rj8JTP1teLF8P8q7UJwGSXqjjYH?cluster=devnet) |
| Run anchored as a Solana Memo (payload = run-manifest hash) | [`5xNkS5XW…BnCVy`](https://explorer.solana.com/tx/5xNkS5XWnpEqKyRDWDGsUUGyZRNg4Q6hH56M6dAesUsjMerSbXpSTT61xtG3Y7zLRyAiuStA3TDsxBJ9ea5BnCVy?cluster=devnet) |

We also verified live that TxLINE's **StablePrice odds are de-margined consensus** (outcome probabilities sum to ~100%) — the clean fair-value input CLV scoring needs — and that anchoring a run confirms in **~1.3 seconds**.

---

## How it works (the proof loop)

```
        ┌─────────────────────────────────────────────────────────────────────┐
        │  TxLINE live odds  →  de-margined CONSENSUS FAIR PROBABILITY (sealed) │
        └─────────────────────────────────────────────────────────────────────┘
                                       │
              (1) PROPOSE              ▼                       trust path = ZERO LLM imports
        ┌──────────────────┐   constrained AgentAction   ┌────────────────────────────────┐
        │  Agent (template │ ─────────────────────────▶  │  (2) DETERMINISTIC LAW          │
        │  + pinned config)│   {market, side, params}    │  recompute edge + CLV from      │
        │  claimed_edge =  │                              │  the SEALED evidence only       │
        │  UNTRUSTED       │                              │  (law/recompute.py)             │
        └──────────────────┘                              └────────────────────────────────┘
                                                                 │
                                       (3) GATE + EXECUTE        ▼
                                ┌─────────────────────────────────────────────┐
                                │  Two-phase POLICY gate                        │
                                │  pre-quote: kill-switch · stake · allowlist   │
                                │            · circuit breaker · live cap       │
                                │  post-quote: staleness · slippage · exec-edge │
                                │  → VENUE (Polymarket) · receipts NON-SCORING  │
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
        │  manifest_hash  →  Solana Memo anchor   ·   POST /runs/{id}/verify               │
        │  re-runs the law over sealed evidence → per-check verdict → "✓ / ⚠ NOT verified" │
        └───────────────────────────────────────────────────────────────────────────────┘
```

The killer flow a judge can click: **Studio (pin a config → Deploy) → Live Cockpit → an `AGENT_ACTION` → the Decision Inspector (the LLM proposal fenced "NOT AN INPUT TO SCORE", beside the law's recompute) → the Proof Card → Verify.** One flow: `configure → preflight → deploy → observe → verify`.

### The flagship strategy — Sharp Momentum v2

A **false-positive-controlled line-movement detector**, not an oracle: logit-space movements → EWMA smoothing → robust-z with a scale floor → **directional** Page-Hinkley confirmation → persistence → per-market cooldown, with warmup gates and all ten behavioral parameters folded into `config_hash`. Grounded in the line-movement literature (Simon 2024, *Management Science*) and validated against operating-curve tapes: quiet on noise and drift, silent on single outliers, fires on sustained sharp repricing. It *proposes only* — the law scores.

---

## The 7 proof checks

Every run produces a frozen, 7-member `CheckId` block. Checks are **structural and falsifiable** — each recomputes from sealed evidence and returns `pass / fail / pending / not_applicable`, **never a hardcoded PASS**. Checks are *not* metrics: CLV is a metric, never a check.

| Check | What it proves |
|---|---|
| `evidence_integrity` | The recomputed `evidence_hash` matches the sealed run-event prefix — no byte was altered. |
| `llm_boundary` | No LLM SDK ever entered the trust path (import-audited; **fails closed** if a trust dir is missing). |
| `metrics_recomputed` | CLV/scores re-derived from the *sealed* payloads match the persisted scores — tamper a score row and this fails. |
| `manifest_bound` | The proof manifest binds run_id, evidence root, and score root together. |
| `policy_obeyed` | Every execution passed the two-phase policy gate (no decision bypassed the law). |
| `receipt_separation` | Off-chain venue receipts are **non-scoring** — a receipt can never become proof evidence. |
| `anchor` | The manifest hash is committed to Solana (honest `pending`/`not_applicable` when offline). |

## Why it's tamper-evident

- **Evidence is sealed before it's scored.** `evidence_hash` covers only the sealed `run_events` *prefix*; the derived tail (scores, receipts, telemetry, route events) lives *outside* the hash. Change a sealed input → `evidence_integrity` fails.
- **Scores are re-derived, not echoed.** Verify rebuilds the checks *fresh*, so doctoring a persisted `score_row` is caught by `metrics_recomputed` even though the seal is intact. We tested exactly this: tampered `clv_bps` → fail.
- **The LLM's claim can't become the score — by construction.** Scored CLV is read from the law's recompute; `claimed_edge_bps` lives in a separate untrusted block. Two code paths, no flow between them.
- **A Merkle root-forest** (event-log / score / receipt / policy / competition / payout domains) is bound into the manifest, and the manifest hash is **anchored on Solana**.
- **Verify is honest about scope.** Top-level `verified` reflects evidence-prefix integrity; the per-check block carries the full verdict — "⚠ NOT fully verified" renders when a blocking check fails even with an intact seal. No false green.

### The trust boundaries we hold (the discipline)

Enforced by tests + an import-audit, not good intentions: checks ≠ metrics (CLV never a check) · no hardcoded PASS · runtime telemetry never hashed/sealed/scored · receipts non-scoring · ranking is CLV-only (Kelly never a metric) · honest states (no fabricated data) · pre-run config pinning · runtime-neutral proof contracts · no secrets in repo/image · **CLV confidence keys off scored picks, never law-valid abstentions**.

### Strategy doctrine (why the numbers are honest)

| Quantity | Meaning | Role |
|---|---|---|
| **Fair Value** | TxLINE de-margined consensus probability | market-implied, **not** "guaranteed truth"; never re-de-vigged |
| **Mispricing Gap** | fair vs. venue-implied probability, in bps | probability-space *dislocation* — never an edge, never a score |
| **Executable Edge** | forward EV at the actual venue price, **for the size that submits** | **gates** execution; renders **only** when a genuine venue quote backs it; never scored |
| **CLV** | closing-line value, recomputed from sealed entry vs. close | **the only scored metric** |
| **Stake · Kelly** | capped fractional Kelly under the policy envelope | **policy sizing only**; never a score/rank input |

---

## The mode ladder — how real money stays fail-closed

Five modes, honestly labeled, never conflated: **Replay · Backtest · Paper · Dry-run · Live-guarded.** The default is always dry — the safe state is the state you get by doing nothing.

A real Polymarket order requires **every** gate below; miss any one and the run degrades to a dry simulation that *records why* (`degraded_because_not_armed: live_ready_false | missing_live_deps | non_real_adapter`):

1. **Mode is exactly `live_guarded`** — a structural conjunct, so no other mode can arm even with full credentials supplied.
2. **The route arm gate** — an operator-supplied adapter bundle with `live_ready == True`. `live_ready` itself requires two **operator-verified** facts (the on-chain neg-risk approval and a 1-share FAK smoke) to be *explicitly* confirmed — a preflight that merely "didn't object" is not consent.
3. **The adapter's own lock** — write-enabled settings, `dry_run=False`, and an injected write client, checked again at submit time. Two independent locks; the money gate doesn't trust the routing layer.
4. **At run time** — a circuit breaker (OPEN = deny with zero venue I/O; only *executed* failures trip it), a live-only stake cap, quote-size coupling (the quoted edge is priced for the exact size that submits), and a resolver that raises `MarketUnavailable` rather than guess a token (draw maps to YES on the draw-binary market; an away bet maps to the away-*wins* token — a swap our own review caught while it was still latent, before it could ever touch money).
5. **No HTTP path arms real money.** The API deliberately passes no live deps — real execution is operator-direct-only, by construction. See the [operator runbook](docs/operator-runbook.md).

Every gate above was built test-first and passed independent adversarial review before the path to real funds was declared operator-ready. **No real order has been placed yet** — the first 1-share smoke is deliberately a human decision, not an agent's.

---

## What's built vs. what's next

Shipped through **six reviewed plans (~130 tasks)** — every task landed via a strict two-stage review (spec-compliance, then code-quality), with **independent adversarial cross-model review at every milestone gate**, genuine RED→GREEN TDD, and a byte-for-byte golden suite guarding the sealed path throughout.

### Built (this hackathon)

| Area | What shipped |
|---|---|
| **Proof engine** | The 7-check taxonomy, deterministic law, CLV scoring, Merkle root-forest, Solana Memo anchor, two-phase policy gate, live TxLINE auth/odds client, incremental sealed runtime with persist-then-broadcast. |
| **Verify (one flow)** | `POST /runs/{id}/verify` recompute-from-sealed-evidence — identical for arena runs, deployed agents, and backtests. |
| **Agent Studio deploy loop** | `configure → preflight → deploy → observe → verify`: typed+bounded configs (invalid values fail preflight with named reasons — never a "weird but hashable" instance), async deploy returning `run_id` before seal, and a **durable store-backed AgentInstance** (Postgres/in-memory) pinning config/policy hashes, mode, allowlists, run link, status, and the named preflight audit. |
| **Sharp Momentum v2** | The flagship detector — deterministic, no-lookahead-proven, 10 params config-hashed, literature-grounded, operating-curve tested. |
| **Venue: Polymarket** | Read + write paths on the real CLOB (vendored, pinned client), an event→market resolver grounded in live-verified market structure (1X2 per-team, draw-binary, O/U multi-slug), price-unit discipline (native price audit-only), honest fill semantics, and a preflight with operator-verify tri-state — "couldn't check" never reads as "passed". |
| **Execution safety** | The full fail-closed live_guarded surface: structural arm conjunction, two independent locks, `live_ready`, circuit breaker + live cap, quote-size coupling, earned `real_venue_quote`, honest degrade with recorded reasons. |
| **Backtest + replay** | Content-hashed ReplayPacks from recorder sessions or real TxLINE history, a deterministic BacktestRunner with honest mode labels, and the real-fixture pipeline behind the 18-fixture Run-001 / Run-002 results above (raw vendor odds stay local — licensed data). |
| **Judge demo** | `python scripts/demo_phase2d.py` — offline, deterministic, produces *real sealed runs* + a manifest of verify URLs; synthetic data self-labels on every surface. |
| **Frontend** | The full product surface: Live Cockpit, Decision Inspector (untrusted-LLM fence), Proof Card + Verify, Leaderboard, Agent Studio (source-mode choice, honest pin affordance, real deploy), Operator Dashboard, Markets, directories, H2H Duel, Ops drawer, mobile — token-disciplined throughout. |
| **Agent SDK** | `veridex-agent` CLI + typed config + `Dockerfile.agent` — the standalone core is the *same single runner seam* the deploy endpoint uses. Secret-safe. |

### What's next (honest scope)

- **Operator steps (gated, human-only):** the 1-share FAK smoke → `live_ready`; wallet funding + USDC/CTF approvals; a Postgres AgentInstance round-trip on the first real deploy. See the [operator runbook](docs/operator-runbook.md).
- **Falsify the Run-002 hypothesis (C/P2 — Polymarket longshot-divergence falsification):** all-outcome Polymarket normalization; measure divergence over *all* matched decisions (not just drift's fired picks); a later-price convergence test; bid/ask/depth instead of mids; a larger fixture sample. The honest next experiment — to falsify-or-promote the longshot ramp, not to assume it. (The `CumulativeDriftAgent` template and the market-quality eligibility filter that Run-001 rests on are **already built**; predeclared in-play evaluation windows remain ahead.)
- **Custody / payouts:** the Prize Vault remains designed-and-visible, not wired — no fabricated payouts anywhere.
- **Control plane:** a hosted runtime/control-plane phase *under* the proof boundary — sessions/memory/tracing as observability, never evidence.
- **Beyond:** mainnet anchoring, a public verifier explorer, more venue adapters behind the same fail-closed `VenueAdapter` seam.

---

## Quickstart

```bash
# --- The judge demo: real sealed runs, offline + deterministic ---
cd veridex-arena
python -m venv .venv && source .venv/bin/activate
pip install -e ".[api,agent,live]"
pytest -q                                        # 1,000+ tests, fully offline

python scripts/demo_phase2d.py --serve           # flagship backtest + paper run → demo_manifest.json
                                                 # then open the printed /runs/{id}/verify URLs
                                                 # (synthetic pack — labeled synthetic on every surface)

# --- The API + the arena ---
uvicorn veridex.api.router:app --reload          # then, in another shell:
curl -X POST localhost:8000/demo/run             # runs agents, seals, scores, anchors
curl localhost:8000/leaderboard                  # CLV-ranked
curl -X POST localhost:8000/runs/<run_id>/verify # ← re-proves the run from sealed evidence

cd apps/web && pnpm install && pnpm dev          # http://localhost:3000
# → Agent Studio → pin a config → Deploy (replay mode works end-to-end in the default app,
#   no credentials needed) → watch it in the Cockpit → Verify the sealed run.

# --- Deploy your own agent (SDK) ---
veridex-agent run --config veridex_agent/sample_agent.toml   # prints [VERIFIED] + the evidence_hash
docker build -f Dockerfile.agent -t veridex-agent .          # secrets via --env-file, never baked in
```

> [!IMPORTANT]
> Live TxLINE/Solana runs need credentials (typed config / env only — never committed). Real-money execution additionally requires the operator-only arming steps in the [runbook](docs/operator-runbook.md); nothing in this repo places a real order on its own.

---

## Project structure

```
veridex-arena/
├── veridex/              # Python backend — the proof engine
│   ├── law/              #   deterministic recompute — the trust core
│   ├── checks/           #   the 7-check taxonomy (zero LLM imports)
│   ├── verifier/         #   recompute-from-sealed verify + proof cards
│   ├── policy/           #   two-phase gate · envelope · circuit breaker
│   ├── strategies/       #   momentum v1/v2 · value · sharp stats
│   ├── venues/           #   VenueAdapter seam · Polymarket read/write · resolver · preflight
│   ├── execution/        #   the execution lane (receipts non-scoring) · edge legibility
│   ├── backtest/         #   BacktestRunner + honest-mode reports
│   ├── deploy/           #   deploy preflight · durable AgentInstance
│   ├── competition/      #   live arena service · sealed events · operator live-path routing
│   ├── ingest/           #   TxLINE client · ReplayPacks · feed health
│   ├── chain/            #   Merkle root-forest + Solana Memo anchor
│   └── api/              #   the FastAPI proof surface
├── apps/web/             # Next.js frontend — Studio · Cockpit · Inspector · Proof Card
├── veridex_agent/        # deploy-your-own-agent SDK (the same single runner seam)
├── scripts/              # demo_phase2d.py (judge demo) · polymarket_smoke.py (operator-gated) · txline_live/
├── tests/                # 1,000+ backend tests incl. a byte-for-byte golden seal suite
└── docs/                 # deploy-judges · operator-runbook · txline-feedback · FAQ
```

---

## Testing & quality

Veridex's correctness *is* its product, so the bar is high:

- **1,000+ backend tests and ~420 frontend tests** — genuine RED→GREEN TDD; trust-bearing tests are revert-proofed (we broke the code on purpose to confirm the test catches it).
- **A byte-for-byte golden suite** guards the sealed path — every refactor since the baseline has left the sealed bytes provably identical.
- **A trust-path import-audit** asserts zero LLM SDK in the law/checks/scoring/verifier/policy path.
- **Two-stage review on every task** (spec-compliance, then code-quality) plus **adversarial cross-model review at every milestone gate.** The process caught real defects before they could matter — among them: a fabricated preflight pass, a confidence metric that counted abstentions as sample, a deploy loop that only worked with test-injected dependencies, and a market-resolver mapping that would have sent an away bet to the away-*loses* token. Every one is now a regression test.
- Strict `mypy` + `ruff` + `tsc` + `eslint`; build-enforced design-token conformance on the frontend.

---

## Why Veridex

Autonomous trading agents are coming. The blocker isn't capability — it's **trust**: an agent that grades itself is worthless, and "trust me" doesn't scale to money. Veridex is the missing layer — a **proof substrate with a deployment loop on top**, so anyone can configure an agent, run it against real markets under real guardrails, and hand a third party the tools to check the result. Performance becomes something you *verify*, not something you *believe* — win, lose, or honest abstention.

**Agents can trade. They can't grade themselves. Veridex grades them — and lets you check the grade.**

---

<div align="center">
<sub>Built for the TxLINE / TxODDS World Cup hackathon — Agents &amp; Trading track (Solana) · proof-first · honest-by-construction · nothing you can't verify.</sub>
</div>
