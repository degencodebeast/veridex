# Deploy Your Own Veridex Agent (WD-3)

Run ONE agent on TxLINE under the same law / policy / proof seal as the Veridex arena — but
outside the competition container — producing an independently-verifiable proof (anchored on
Solana when anchoring credentials are configured; the replay quickstart below honestly reports
`anchor=not_anchored`).

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

## Templates and config

A Veridex deploy is a pinned strategy instance:

```text
AgentTemplate + AgentConfig + PolicyEnvelope = AgentInstance
```

- **AgentTemplate** is the strategy family, such as value-vs-venue, stale-line, sharp-momentum,
  arb scanner, or market-maker/QuoteGuard.
- **AgentConfig** is the concrete strategy instance: market universe, signal thresholds, warmup /
  lookback windows, confirmation rules, quote freshness, liquidity/spread requirements, stake
  sizing, risk caps, cooldowns, source mode, and execution mode.
- **PolicyEnvelope** is the risk boundary: stake caps, order caps, allowlists, kill switch, quote
  freshness, and approval requirements.

Changing a strategy-affecting config value creates a new pinned config hash. That is intentional:
two configs on the same template can produce different CLV, PnL, and drawdown, and Veridex should
be able to replay which exact config caused which actions.

Configs can change trading behavior and profitability. They cannot change Veridex's trust rules:
no config may bypass law/recompute, policy, evidence integrity, Checks, receipt separation,
runtime/proof separation, or scoring immutability.

Before guarded live execution, run the config through replay/backtest, live-paper, and dry-run so
the strategy has a visible performance and policy record before real venue submission.

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
