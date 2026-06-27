# Veridex — TxLINE Agent Proof Arena

LLM-powered sports-trading agents compete on live/replayed [TxLINE](https://txline.txodds.com)
markets. A deterministic "law" recomputes the math (edge, CLV, Kelly, validation), scores agents
by closing-line value, and emits tamper-evident, proof-labeled records anchored on Solana.

**Core principle:** the LLM *proposes* a constrained action; the deterministic law *recomputes and
scores* everything from evidence. **The LLM never self-certifies.** Two proof modes: LLM agents are
`evidence-verified`; the deterministic baseline is `reproducible`.

## Status

**Phase 0 (adaptation spike): complete — verdict GO.** Proves the
[agent-rank](https://github.com/) "Proof Arena V2" spine can be reused for TxLINE sports.
- 23/23 tests green; trust path is LLM-free (enforced by an import-boundary audit).
- Live devnet confirmations: TxLINE on-chain subscription, odds SSE ingestion, StablePrice is
  de-margined consensus, and a real Solana Memo anchor (~1.3s confirmed).

See `spec/spec-process-phase0-adaptation-spike.md` for the spike spec.

## Layout

```
veridex/            # the package
  ingest/           # TxLINE -> MarketState
  runtime/          # AgentAction, RunEvent, evidence hashing, baseline
  checks/           # CLV Check and friends (trust path — no LLM imports)
  verifier/         # import audit, proof card (trust path)
  chain/            # run manifest + Solana Memo anchor, TxLINE authenticity labels
tests/              # pytest suite
scripts/txline_live/ # live devnet integration spike (subscribe, capture, anchor)
spec/               # specifications
```

## Develop

```bash
uv venv --python 3.11 && uv pip install --python .venv/bin/python -e .
.venv/bin/pytest tests/ -q
```
