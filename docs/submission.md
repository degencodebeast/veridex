# Veridex — Hackathon Submission

**Track:** TxLINE / TxODDS World Cup hackathon — Agents & Trading (Solana)

> **Veridex is the proof-and-deployment layer for autonomous sports-trading agents: configure an agent from a strategy template, deploy it into a live arena to compete head-to-head on TxLINE markets, let it trade under policy guardrails — then verify what actually happened from sealed evidence, on Solana, whether it found an edge or not.**

---

## The problem we chose

Autonomous trading agents have a trust problem, not a capability problem. Every "my AI bot made 40%" claim is unverifiable — the model could be cherry-picking, peeking at the future, or lying — and the agent that *reports* its performance is the same agent being *graded*. Nothing built on "trust me" scales to money.

**Our insight: separate the agent from its own grading, structurally.** The agent may only *propose*; a deterministic law recomputes every number from sealed evidence; policy decides whether acting is safe; the venue executes; and anyone — including a judge — can re-run the proof.

```
AGENT proposes → LAW recomputes → POLICY gates → VENUE executes → PROOF verifies → LEADERBOARD ranks
```

## What we built

Four pillars, one product:

1. **Agent Studio** — configure and deploy agents from strategy templates. Typed, bounded configs (invalid values fail preflight with named reasons); one flow: `configure → preflight → deploy → observe → verify`. A deploy pins `AgentTemplate + AgentConfig + PolicyEnvelope + Evidence = AgentInstance` into a durable, store-backed record — two users can deploy the same template with different configs, get different behavior, and Veridex still proves exactly what each did.
2. **Live Agent Arena** — a real-time room where agents **compete head-to-head on identical sealed inputs**: same ticks, same law, same policy, so rank differences are strategy, never luck of the feed. Agents ingest TxLINE de-margined odds, propose actions, rank on closing-line value, and stream their full decision trail (proposal → law recompute → policy verdict → receipt) — with a Head-to-Head Duel view and a falsifiable leaderboard; deploy your own agent through the same pipeline and rank on the same leaderboard.
3. **Verification / proof layer** — sealed evidence hashes, seven structural checks that recompute rather than assert, a Merkle root-forest, Solana anchoring, and `POST /runs/{id}/verify`: tamper with one sealed byte and the proof goes red. The LLM's claimed edge is fenced as `NOT AN INPUT TO SCORE`; the trust path is import-audited to contain zero LLM SDK code.
4. **Execution + risk layer** — a real Polymarket integration (read + write paths, an event→market resolver that fails closed rather than guess a token) behind a two-phase policy gate, circuit breaker, stake caps, quote-size coupling, and five honestly-labeled run modes. The path to real money is fail-closed and **operator-only by construction** — no HTTP call or agent can arm it.

## The five-minute judge demo

```bash
cd veridex-arena
python -m venv .venv && source .venv/bin/activate && pip install -e ".[api,agent,live]"
pytest -q                              # 1,000+ tests, fully offline
python scripts/demo_phase2d.py --serve # real sealed runs + a manifest of verify URLs
```

Open the printed `/runs/{id}/verify` URLs — each re-runs the deterministic law over sealed evidence and returns a per-check verdict. Then the product surface:

```bash
cd apps/web && pnpm install && pnpm dev   # http://localhost:3000
```

**The click path:** Agent Studio → pin a config → **Deploy** (replay mode runs end-to-end in the default app, no credentials) → watch it in the **Cockpit** → open an action in the **Decision Inspector** (the LLM's proposal fenced beside the law's recompute) → **Proof Card** → **Verify**. Full steps: [`docs/deploy-judges.md`](deploy-judges.md).

On-chain evidence (Solana devnet): a TxLINE data subscription and a run-manifest anchor, both clickable in the README's "Proof it's real" table. Anchor confirmation measured at ~1.3 s.

## Results — what we ran on real World Cup data

We pulled real TxLINE history across **18 finished World Cup fixtures**, sealed each into a content-hashed ReplayPack, and ran two experiments end-to-end through the pipeline. Both are recorded in the research log; the honest framing is baked in.

**Run-001 — a candidate CLV signal (the hook).** The `CumulativeDriftAgent` (the drift template for smooth multi-day repricing) averaged **+61.19 bps CLV** across the 18-fixture filtered universe, **beating all three acting deterministic baselines** on this sample (favorite +4.6, threshold-move −126.5, seeded-random −341.7 bps), net-positive on 10 of 18 fixtures. This is a **candidate rung-1 CLV signal, not proven executable alpha** — CLV is measured against the sharp TxLINE close, no venue leg, effective n ≈ 18 fixtures, so it is *directional evidence, not a statistical proof.* What makes it credible is not the size of the number but its provenance: the agent beat its baselines on identical sealed inputs, and the CLV was **recomputed by the law, never self-reported.**

**Run-002 — the trust moat.** We then tested whether that candidate translated into **Polymarket venue edge** — and the platform prevented an overclaim. The rung-2 venue lane **works**: it priced **94.7% of drift's in-scope 1X2 decisions** against time-aligned Polymarket mids under a bounded-staleness bound, with a self-verified pinned coverage hash and `venue_source_id`. But the rung-2 *estimated mids* result was a near-perfect **monotonic longshot ramp** — **+607 bps** at 0–20% probability decaying to **+33 bps** at 80–100% — the fingerprint of a favorite-longshot / de-margin-scale divergence, **not** strategy-specific dislocation. So **Run-002 did NOT demonstrate executable venue edge.** These are estimated mids, not fills; `real_executable_edge_bps = None`; no profit and no fillability claim.

**Why this is the product working.** It would have been trivial to headline "+607 bps." Instead the law priced the decisions, the `VenueBehaviorReport` slices surfaced the ramp, and Veridex labelled it a structural divergence to *falsify* — not alpha to trade. Most agent demos ask a judge to trust the bot; a trustworthy **"no executable edge yet"** is part of what Veridex ships. Some entrants anchor self-generated numbers on-chain, or call their pricing "cryptographically auditable" with no on-chain component at all; Veridex instead verifies its inputs against TxLINE's real published root. Full run-notes live in the research record (`.omc/research/run-001-run-note.md`, `run-002-run-note.md`).

> **C/P2 — Polymarket Longshot Divergence Falsification** (future work): all-outcome Polymarket normalization; measure divergence over *all* matched decisions (not just drift's fired picks); a later-price convergence test; bid/ask/depth instead of mids; a larger fixture sample. The honest next experiment — to falsify-or-promote the Run-002 hypothesis, not to assume it.

## Why it stands up to scrutiny

- **It answers the track literally — including its own starter ideas.** The track's "ideas to get started" #1 (*a sharp-movement detector that flags odds shifts and tracks whether it predicted the outcome*) is Sharp Momentum v2 + CLV scoring; idea #2 (*an agent-vs-agent arena on the same TxLINE feed where the better strategy wins*) is our head-to-head arena on identical sealed inputs. We built both — then added the layer that makes them trustworthy: *verifiability*. And because the demo is deterministic replay over sealed data, judges can re-run and re-verify it **after the World Cup ends** — exactly the review constraint the brief calls out.
- **The proof is falsifiable, not decorative.** Verify recomputes; checks fail on tampering (we tampered on purpose to prove it); receipts are structurally non-scoring; the leaderboard can't be gamed by abstaining or by an agent's own claims.
- **Authentic inputs, not just faithful math.** Beyond re-computing every result with a neutral law, Veridex checks that the odds it sealed are the genuine values TxLINE published — verifiable against TxLINE's Merkle root (269/270 of sampled odds returned valid inclusion proofs). So "the inputs weren't doctored" is something a reviewer can check, not something we ask them to trust.
- **It's a platform and a competition, not a demo bot.** Templates + typed configs + pinned instances + a durable deploy loop + an SDK that runs *your* agent through the same sealed pipeline — into an arena where agents race on identical inputs and the leaderboard is a fair fight by construction.
- **Real-venue execution is engineered like money is real** — because it is. Two independent locks, a structural mode conjunct, operator-verified `live_ready`, a circuit breaker, and a resolver that raises rather than guesses. Our own review process caught a side↔token swap that would have sent an away bet to the away-*loses* token — while it was still latent. It's a regression test now.
- **Process you can audit:** ~130 tasks across six reviewed plans, each through two-stage review, with independent adversarial cross-model review at every milestone gate; 1,000+ backend tests, ~420 frontend tests, and a byte-for-byte golden suite proving the sealed path never drifted.

## Honest scope (what is *not* claimed)

- **No real-money order has been placed yet.** The live_guarded surface is built, reviewed, and fail-closed; the first 1-share smoke is deliberately a human operator's decision ([operator runbook](operator-runbook.md)).
- **No executable venue edge is claimed.** Run-001 is a *candidate* rung-1 CLV signal (not proven executable alpha); Run-002 did NOT demonstrate executable venue edge (rung-2 estimated mids, `real_executable_edge_bps = None`, no profit or fillability claim). The C/P2 falsification lane that follows is predeclared, not retro-fitted.
- **Custody/payouts (Prize Vault)** are designed and visible in the UI but not wired — and the UI says so.
- **Raw vendor odds are not redistributed** — the real-fixture ReplayPack stays local (licensed data); the pipeline, fixes, and sealed-proof discipline are what ship.

## Stack

Python 3.11 · FastAPI + Pydantic v2 · Solana (Memo anchor, devnet) · TxLINE StablePrice (de-margined consensus) · Polymarket CLOB (vendored, pinned client) · Agno + OpenRouter for the propose-only LLM layer (outside the audited trust path) · Next.js / React / TypeScript strict.

## TxLINE endpoints used

| Endpoint | How Veridex uses it |
|---|---|
| `POST /auth/guest/start` | Guest JWT mint/refresh for all data calls |
| `POST /api/token/activate` | Activates the API token after the on-chain devnet subscription |
| `GET /api/fixtures/sports/competitions` | Fixture bundle — fixture ids, kickoff times, team identities (drives market resolution) |
| `GET /api/odds/stream` (SSE) | Live StablePrice odds into the live runner / recorder |
| `GET /api/odds/updates/{fixtureId}` | Full odds movement history for a fixture — the backbone of ReplayPacks and the real-fixture backtest (65k+ updates for one match) |
| `GET /api/odds/snapshot/{fixtureId}?asOf=` | Point-in-time odds (closing-line reconstruction, historical probes) |
| `GET /api/odds/validation` | Merkle proof of an odds message (data-integrity probe) |
| `GET /api/scores/stream` (SSE) · `GET /api/scores/updates/{fixtureId}` | Live/updated scores — match phase (pre-match vs in-running) for honest closing-line semantics |

Integration experience feedback (what worked, where we hit friction): [`docs/txline-feedback.md`](txline-feedback.md).

## Links

- **README** — the full story: [`README.md`](../README.md)
- **Judge demo steps** — [`docs/deploy-judges.md`](deploy-judges.md)
- **Operator runbook (real-money path)** — [`docs/operator-runbook.md`](operator-runbook.md)
- **Deploy your own agent** — [`docs/deploy-your-own-agent.md`](deploy-your-own-agent.md)
- **TxLINE integration feedback** — [`docs/txline-feedback.md`](txline-feedback.md)
- **FAQ** — [`docs/faq.md`](faq.md)

---

*Agents can trade. They can't grade themselves. Veridex grades them — and lets you check the grade.*
