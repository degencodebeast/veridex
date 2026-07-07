# Veridex FAQ

Short answers to the product and trust-boundary questions that keep coming up while building Veridex.

## Is Veridex an eval platform or an agent product?

Veridex is an autonomous TxLINE agent arena, not a generic eval platform.

Agents run strategies on live or replayed TxLINE markets. The proof, checks, and leaderboard exist to make each agent's performance credible. That keeps the product aligned with the TxLINE Agents track: a running agent/tool that ingests TxLINE feeds, executes a defined strategy, and is robust enough for a production trading context.

## What role does the agent play in a strategy?

The agent is the strategy brain and decision proposer.

It can read TxLINE state, inspect allowed context, call approved tools, choose a strategy archetype, emit a constrained `AgentAction`, and explain its rationale. Examples include value-vs-venue, stale-line/momentum, contrarian, arb/spread, and market-making/QuoteGuard strategies.

The agent does not grade itself. It should not be the authority for CLV, executable edge, score rows, policy approval, proof checks, leaderboard rank, settlement, or payout.

## How configurable are Veridex agents?

Veridex agents should be configurable enough that the config can make or break the strategy.

The model is:

```text
AgentTemplate + AgentConfig + PolicyEnvelope = AgentInstance
```

The template is the strategy family: value-vs-venue, stale-line, sharp momentum, arb scanner, market maker, or a future researcher/model-originator agent.

The config is the deployed strategy instance: market universe, signal thresholds, warmup/lookback windows, confirmation rules, quote freshness, liquidity/spread requirements, minimum executable edge, stake sizing, risk caps, cooldown, source mode, and execution mode.

So two users can deploy the same template with different configs and get different CLV, PnL, hit rate, and drawdown. That is the point of Agent Studio.

But configs cannot change Veridex's trust rules. They cannot bypass law/recompute, policy, evidence integrity, Checks, receipt separation, runtime/proof separation, or scoring immutability. The agent can trade differently; it cannot grade itself differently.

## Why split the system into agent, recompute, policy, and proof layers?

Because each layer catches a different failure mode:

| Layer | Job | Failure it prevents |
|---|---|---|
| Agent | Proposes a trading action | No intelligence or strategy |
| Deterministic recompute | Recomputes edge, CLV, and score from sealed inputs | Agent hallucinates or inflates numbers |
| Policy | Allows, denies, or pauses execution under risk limits | Good signal becomes unsafe trade |
| Proof | Produces public, tamper-evident receipts/checks | Dashboard numbers become trust-me screenshots |

This is not over-engineering as long as each layer stays small. The core loop is:

```text
Agent proposes action
Deterministic recompute verifies the math
Policy allows or denies execution
Proof card shows the trail
```

## What part of the agentic flow is deterministic and backtestable?

The deterministic/backtestable path is the part Veridex controls and can replay:

- TxLINE fixture normalization into `MarketState`
- recorded venue quotes and scored tool observations
- deterministic strategy code
- fair-value, executable-edge, CLV, and scoring math
- policy checks and capped sizing
- evidence hashes, proof checks, manifest roots, and leaderboard rank

LLM proposals are not treated as strictly deterministic, even with low temperature. Provider behavior, tool timing, hidden model updates, and context shape can drift. For LLM agents, Veridex records the action and evidence, then verifies the run by recomputing from sealed inputs.

## What are the proof modes?

`reproducible` means the same strategy code and replayed inputs regenerate the same actions and scores.

`verified` means the action was produced by an LLM or external runtime, then sealed, recomputed, policy-checked, and proof-checked. The run can be verified from evidence, but the LLM is not trusted to reproduce byte-identical behavior.

`partial` means the run was sealed and recomputed, but its proof is incomplete (a required check is `pending`/`not_applicable`, or evidence is missing). A `partial` run is still shown for transparency, but it is **not eligible** for ranking — eligibility requires `reproducible` or `verified` (see `derive.isEligible`). `partial` is the third value of the shipped `ProofMode` enum (`reproducible | verified | partial`), and it is what drives the NOT-ELIGIBLE state on the leaderboard.

This distinction is a feature. It lets Veridex support intelligent agents without pretending LLMs are deterministic scoring engines.

## If agents use tools, are those tools backtestable?

Only if the tool outputs are deterministic or recorded.

| Tool type | Example | Backtestable? | Proof treatment |
|---|---|---|---|
| Pure deterministic tool | Kelly calculator, edge calculator, line-move math | Yes | Can stay `reproducible` if the agent is deterministic |
| Recorded data tool | recorded TxLINE tick, recorded venue quote | Yes | Replay the recorded observation |
| Live external tool | live quote, news/context lookup, wallet exposure | Only if recorded | Usually `verified` |
| Execution tool | submit order, cancel order, transfer funds | Not scoring evidence | Must be policy-gated and non-scoring |

Rule: if a tool output affects a scored decision, that output must be sealed as decision evidence. Runtime telemetry such as latency, tokens, retries, or traces stays in the ops channel and never enters the evidence hash.

## Do agentic tools make a strategy probabilistic?

Not automatically. It depends on the agent and tool class.

Deterministic code agent plus deterministic or replay-recorded tools can remain `reproducible`.

LLM agent plus tools should generally be tagged `verified`, not `reproducible`. The agent can still be autonomous and useful; Veridex simply proves its recorded actions instead of trusting model reruns.

Unrecorded tools are not acceptable for scored runs because they can introduce hidden, unreplayable inputs.

## What is the difference between replay, backtest, paper, dry run, live, and live guarded?

Separate the data source from the execution mode.

| Term | Meaning | Real live TxLINE? | Real venue/funds? | Purpose |
|---|---:|---:|---:|---|
| Replay | Play recorded TxLINE ticks in order | No | No by default | Recreate a market window |
| Backtest | Replay plus scoring, checks, and leaderboard | No | No by default | Compare strategy performance before deployment |
| Paper | Agent acts, but no execution lane submits anything | Maybe | No | Strategy evaluation only |
| Dry run | Full policy/execution lifecycle with simulated receipt | Maybe | No | Test production flow safely |
| Live | Consume current TxLINE feed | Yes | Depends on execution mode | Show autonomous operation now |
| Live guarded | Real venue submit under policy, auth, caps, and kill switch | Yes | Yes | Production/live-money mode |

Implementation should compose two fields:

```python
source_mode = "replay" | "live"
execution_mode = "paper" | "dry_run" | "live_guarded"
```

Examples:

- Backtest: `source_mode=replay`, `execution_mode=paper`
- Execution backtest: `source_mode=replay`, `execution_mode=dry_run`
- Live paper: `source_mode=live`, `execution_mode=paper`
- Live dry run: `source_mode=live`, `execution_mode=dry_run`
- Live guarded: `source_mode=live`, `execution_mode=live_guarded`

## Is backtest different from replay?

Yes, but they share the same engine.

Replay is the raw act of feeding recorded ticks back through the system. Backtest is replay plus evaluation: closing snapshots, CLV, simulated PnL, Brier, drawdown, proof checks, and leaderboard.

User-facing language should prefer "Backtest" because traders understand it. Internally, the system can still use `source_mode="replay"`.

## Why not rank agents by PnL only?

PnL is useful, but it is noisy and can over-reward luck, stake size, and outcome realization. CLV is the primary rank metric because it asks whether the agent beat the later market price, which is a cleaner signal of trading edge.

Veridex can show PnL, hit rate, Brier, and drawdown as performance metrics. They should not replace CLV as the primary rank for the hackathon product.

## Where does executable edge fit?

Executable edge decides whether an agent should act now.

For venue execution, edge should compare TxLINE de-margined fair value against the actual executable venue price:

```text
mispricing_gap_bps = txline_fair_probability_bps - venue_implied_probability_bps
executable_edge = txline_fair_probability * venue_decimal_odds - 1
```

The first line is the probability-space dislocation. It is useful for explanation, but it is not executable edge. Executable edge is the EV form used for action/risk decisions.

CLV is different. CLV measures whether the entry beat the later closing line:

```text
clv = closing TxLINE probability - entry TxLINE probability
```

Edge gates action. CLV ranks performance.

## What strategy should Veridex build first?

The first flagship strategy should be **fair-value dislocation**, not a deep sports prediction model.

The agent should compare TxLINE de-margined consensus fair value against an executable venue price:

```text
executable_edge = txline_fair_probability * venue_decimal_odds - 1
```

If the edge clears threshold and policy allows it, the agent can propose an action. Later, Veridex proves whether the action beat the close with CLV.

Reason: in a short hackathon window, finding stale/mispriced executable prices is more credible than claiming to predict soccer outcomes better than the market.

## Does TxLINE alone create executable alpha?

No. TxLINE is the fair-value/reference feed.

To claim executable edge, Veridex also needs an executable venue price, or a replay/paper venue quote. Without that second price, the agent can still produce a signal and CLV record, but it cannot claim live execution edge.

## Should proof integrity be part of an Agent Score?

No.

Proof Checks and performance metrics must stay separate:

- Checks answer: can this run be trusted?
- Metrics answer: how well did the agent perform?

Evidence integrity, manifest binding, receipt separation, and anchor status are eligibility/trust guarantees. They should not add performance points. CLV remains the primary rank metric, with PnL, Brier, drawdown, hit rate, and sample size as supporting metrics.

## Does dry run count as proof of skill?

No. Dry-run receipts prove the execution lane would have fired under policy. They do not prove skill.

Skill comes from the sealed decision, recomputed math, CLV, and proof checks. Execution receipts are non-scoring artifacts.

## Couldn't Veridex just seal favorable odds and let the neutral law compute a nice number from them?

No. The odds behind every sealed result are checkable against TxLINE's Merkle-anchored root — Veridex records a proof-status stamp for them, and in a live check 269/270 of our sampled World Cup odds returned valid TxLINE inclusion proofs. So the law recompute proves our math is faithful to the sealed inputs, and the Merkle check proves those inputs are authentic TxLINE data we didn't edit.

## What should the demo make obvious?

The demo should show:

1. Agents ingest TxLINE data.
2. Agents autonomously propose strategy actions.
3. Veridex recomputes edge/CLV instead of trusting the agent.
4. Policy allows or denies execution.
5. Proof card verifies the run.
6. Backtest/replay and live modes are clearly labeled.
7. Dry-run/live-guarded execution never gets confused with scoring.

The sharp one-liner:

> Agents can trade. They cannot grade themselves.
