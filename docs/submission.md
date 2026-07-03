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
3. **Verification / proof layer (the moat)** — sealed evidence hashes, seven structural checks that recompute rather than assert, a Merkle root-forest, Solana anchoring, and `POST /runs/{id}/verify`: tamper with one sealed byte and the proof goes red. The LLM's claimed edge is fenced as `NOT AN INPUT TO SCORE`; the trust path is import-audited to contain zero LLM SDK code.
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

## We ran it on real World Cup data — and we're leading with the truth

We pulled the full real TxLINE history for a finished fixture (**USA v Bosnia — 65,156 StablePrice updates over ~6 days**), built a content-hashed ReplayPack, and ran the flagship **Sharp Momentum v2** through the sealed pipeline. Once. No tuning, no fixture shopping. Three things happened:

1. **1X2:** the USA line moved +907 bps — smoothly, over ~1,876 updates (max robust-z 1.13 vs. a 2.5 shock gate). **Zero fires.** Correct behavior for a shock detector — and an honest signal that a shock detector alone under-covers smooth pre-match drift.
2. **Totals:** after the run itself exposed a market-family bug (real TxLINE totals use a key our synthetic data had masked), the fixed detector fired **twice on real data** (z = 3.43, 2.51) — but on O/U 0.5, a near-certain line. A real detection, and a market-selection lesson the strategy owns.
3. **Scoring:** that line's closing price was **suspended** — so the law **refused to fabricate a close** and scored both fires invalid. Result: **avg CLV = None, 0 scored picks, confidence honestly "low"** — and the sealed run still verifies end-to-end.

The run also caught a second bug — the CLV-confidence metric was counting law-valid *abstentions* as sample, so a zero-pick run could read "high confidence." We fixed both surfaces the same day. The synthetic demo tape is labeled synthetic on every machine-readable surface (manifest field, console, pack self-label) — it demonstrates the metric *pipeline*, never real performance.

**Why lead with a null result?** Because it is the product working. Sharp Momentum v2 is a conservative shock detector; on this real pre-match fixture it correctly avoided smooth drift, and the law refused to score suspended closes. That negative result is *why* Veridex reports CLV honestly — and why the platform supports multiple strategy templates (a cumulative-drift template and a market-quality filter are tracked and predeclared) rather than pretending one detector catches every kind of edge. **You cannot fake a win on Veridex — and we didn't fake ours.**

## Why this should win

- **It answers the track literally — including its own starter ideas.** The track's "ideas to get started" #1 (*a sharp-movement detector that flags odds shifts and tracks whether it predicted the outcome*) is Sharp Momentum v2 + CLV scoring; idea #2 (*an agent-vs-agent arena on the same TxLINE feed where the better strategy wins*) is our head-to-head arena on identical sealed inputs. We built both — then added the layer that makes them trustworthy: *verifiability*. And because the demo is deterministic replay over sealed data, judges can re-run and re-verify it **after the World Cup ends** — exactly the review constraint the brief calls out.
- **The proof is falsifiable, not decorative.** Verify recomputes; checks fail on tampering (we tampered on purpose to prove it); receipts are structurally non-scoring; the leaderboard can't be gamed by abstaining or by an agent's own claims.
- **It's a platform and a competition, not a demo bot.** Templates + typed configs + pinned instances + a durable deploy loop + an SDK that runs *your* agent through the same sealed pipeline — into an arena where agents race on identical inputs and the leaderboard is a fair fight by construction.
- **Real-venue execution is engineered like money is real** — because it is. Two independent locks, a structural mode conjunct, operator-verified `live_ready`, a circuit breaker, and a resolver that raises rather than guesses. Our own review process caught a side↔token swap that would have sent an away bet to the away-*loses* token — while it was still latent. It's a regression test now.
- **Process you can audit:** ~130 tasks across six reviewed plans, each through two-stage review, with independent adversarial cross-model review at every milestone gate; 1,000+ backend tests, ~420 frontend tests, and a byte-for-byte golden suite proving the sealed path never drifted.

## Honest scope (what is *not* claimed)

- **No real-money order has been placed yet.** The live_guarded surface is built, reviewed, and fail-closed; the first 1-share smoke is deliberately a human operator's decision ([operator runbook](operator-runbook.md)).
- **No real-market edge is claimed.** The honest real-data result above is the current strategy truth; the strategy roadmap that follows from it is predeclared, not retro-fitted.
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
