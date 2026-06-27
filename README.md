# Veridex

### Can an AI actually beat the market? Veridex makes agents *prove* it — on-chain, no trust required.

LLM agents trade live sports-betting markets. A deterministic "law" recomputes every number, scores
each agent by **closing-line value**, and anchors the whole run on **Solana** — so anyone can verify
the result instead of taking a screenshot's word for it.

**LLM agents. Live odds. Real proofs. Watch them compete.**

---

## The problem

Every "my AI bot made 40%" claim is unverifiable. The model could be cherry-picking, peeking at the
future, or just lying. In trading, **trust-me numbers are worthless.**

## What Veridex does

- **Agents compete** on live [TxLINE](https://txline.txodds.com) odds (the World Cup feed, on Solana devnet).
- An **LLM proposes** a constrained action — the model *never* scores itself.
- A **deterministic law disposes**: it recomputes edge, closing-line value (CLV), and risk *from the
  evidence alone*, and ranks agents on a leaderboard (CLV first; Sim PnL, calibration, drawdown alongside).
- Every run is **anchored on-chain** with a tamper-evident proof and an honest proof badge
  (`evidence-verified` for LLM agents, `reproducible` for the deterministic baseline).

The result: a leaderboard where **rank = provable decision quality**, not vibes.

## Why CLV?

CLV asks *"did the agent get a better price than the market's later consensus?"* — the professional
signal for skill vs. luck. You don't have to wait for a match to finish to know if a decision was good.
PnL and calibration ride along as supporting columns; **proof completeness is an eligibility badge, not
a way to buy rank.**

## Proof it's real (verifiable right now, Solana devnet)

This isn't a slide deck — the on-chain plumbing already works:

| What | On-chain evidence |
|------|-------------------|
| TxLINE data subscription (on-chain `subscribe`) | [`2xmX2caW…qjjYH`](https://explorer.solana.com/tx/2xmX2caWh3U8BGsLcCAatzV48N64x64Xnf2B43Eug5iUnBvGgvm6jnZuZnih6Rj8JTP1teLF8P8q7UJwGSXqjjYH?cluster=devnet) |
| Run anchored as a Solana Memo (payload = run-manifest hash) | [`5xNkS5XW…BnCVy`](https://explorer.solana.com/tx/5xNkS5XWnpEqKyRDWDGsUUGyZRNg4Q6hH56M6dAesUsjMerSbXpSTT61xtG3Y7zLRyAiuStA3TDsxBJ9ea5BnCVy?cluster=devnet) |

We also verified live that TxLINE's **StablePrice odds are de-margined consensus** (the percentages
sum to ~100%) — exactly the clean fair-value input the scoring needs — and that anchoring a run
confirms in **~1.3 seconds**.

## Status

- ✅ **Foundation proven on devnet** — live odds ingestion, the LLM-proposes / law-disposes trust
  boundary, on-chain anchoring, and the honesty rules are validated end-to-end (23/23 tests green).
- 🔨 **In progress** — the full competition: multi-agent runs, the deterministic law's CLV/PnL/risk
  stack, the leaderboard, and a 30-second judge demo.

Built for the **TxLINE / TxODDS World Cup hackathon — Agents & Trading track** (Solana).

## How the trust model works (60 seconds)

```
TxLINE live odds ──► MarketState (immutable snapshot, no future peeking)
                          │
            ┌─────────────┴──────────────┐
   LLM agent (Agno)                Deterministic baseline
   proposes an action              proposes an action
            └─────────────┬──────────────┘
                          ▼
        Deterministic LAW (no LLM, ever) recomputes
        edge · CLV · risk  ──►  score + proof record
                          ▼
        Solana Memo anchor (one per run) + leaderboard
```

The law lives behind an **import boundary**: a static audit fails the build if anything in the scoring
path so much as imports an LLM SDK. The model can't grade its own homework.

## Quick start

```bash
# Python 3.11
uv venv --python 3.11 && uv pip install --python .venv/bin/python -e .
.venv/bin/pytest tests/ -q          # run the suite (offline, deterministic)
```

Live devnet runs need TxLINE + Solana credentials (see `scripts/txline_live/`); the test suite runs
fully offline with committed fixtures.

## Layout

```
veridex/
  ingest/    TxLINE odds  ->  MarketState
  runtime/   agent actions, evidence hashing, deterministic baseline
  checks/    CLV check & friends   (scoring path — no LLM imports)
  verifier/  import audit, proof card  (scoring path)
  chain/     run manifest + Solana Memo anchor, on-chain authenticity labels
scripts/txline_live/   live devnet integration (subscribe, capture, anchor)
spec/                  specifications
tests/                 pytest suite
```

## License

TBD.
