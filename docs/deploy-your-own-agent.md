# Deploy Your Own Veridex Agent (WD-3)

Run ONE agent on TxLINE under the same law / policy / proof seal as the Veridex arena — but
outside the competition container — producing an anchored, independently-verifiable proof.

## Quick start (replay)

```bash
pip install ".[agent,live,api]"
veridex-agent run --config veridex_agent/sample_agent.toml
# [VERIFIED] run_id=… source=replay avg_clv_bps=… manifest_hash=… anchor=not_anchored
```

## Docker

```bash
docker build -f Dockerfile.agent -t veridex-agent .
docker run --rm --env-file veridex/.env -v "$PWD/agent.toml:/app/agent.toml" veridex-agent
```

## Configuration (non-secret only)

The run config (`agent.toml`) holds strategy + policy knobs ONLY. **Credentials never go in the
TOML** (COM-001): the TxLINE JWT + `X-Api-Token`, the Solana keypair (`SOLANA_KEYPAIR_PATH`), and
venue keys are read from `veridex/.env` via `veridex.config.Settings` at use time. See
`veridex_agent/sample_agent.toml`.

## Extension seams

A pro extends the agent through three documented seams:

- **Strategy** — `veridex/strategies/` (e.g. `momentum.py`, `value.py`). A strategy proposes an
  `AgentAction`; it NEVER scores itself — the deterministic law (`veridex/law/recompute.py`)
  recomputes CLV. Add a new strategy and wire it in `veridex_agent/config.py::build_agent`.
- **PolicyEnvelope** — `veridex/policy/envelope.py`. Operator guardrails (stake caps, allowlists,
  min edge, quote freshness, kill switch). Built from your config by `build_policy_envelope`.
- **VenueAdapter** — `veridex/venues/base.py`. The execution surface (quote / submit / status /
  normalize). Only `ExecutionRunner` may reach `submit_order`, and only after the policy gate
  passes — an agent can trade, but cannot unguard itself.

## Trust boundary

Same as the arena: the LLM/strategy proposes → the law recomputes the score from sealed evidence
→ the proof binds the evidence hash → the run anchors on Solana. Verify any run with
`POST /runs/{run_id}/verify` (WD-1) or by re-running `veridex.verifier.recompute.verify_run`.
