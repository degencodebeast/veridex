# Operator Runbook — real-money live_guarded + deploy rehearsal

*Draft. These are the steps that only a human operator can perform — they involve real funds and
on-chain approvals. No agent, automated agent, or automated test in this repo runs a real order; the code is
DRY_RUN by default and every real-money path is gated behind the explicit, operator-only steps below.*

## Safety model (read first)

A real Polymarket order can only be placed when **all** of these hold — each defaults to "off," so the
safe state is the state you get by doing nothing:

1. **Mode** — `execution_mode == LIVE_GUARDED` (default is `dry_run`).
2. **Route arm gate** (`_live_arm_gate` in `veridex/competition/service.py`) — an operator
   `LiveExecutionDeps` bundle is supplied **and** its `live_ready is True` **and** the adapter is a
   genuine real-venue adapter. Miss any → the run **degrades to a dry-run simulation** (Fake adapter,
   `mode="dry_run"`, no breaker) and records an honest `degraded_because_not_armed` reason.
3. **Adapter lock** (`_require_armed` in `veridex/venues/polymarket.py`) — independently, the
   `PolymarketAdapter` refuses to submit unless `POLYMARKET_WRITE_ENABLED` is set, `dry_run=False`, and
   a write client is injected.

These are **two independent locks** (route + adapter) plus the mode. On top of them, at submit time the
circuit breaker, the `max_stake_live_guarded` cap, and the resolver (which raises `MarketUnavailable`
rather than guess a token) all apply. **Start with 1 share.**

---

## Part A — Postgres deploy rehearsal (do this before any live deploy)

The store-backed `AgentInstance` (deploy persistence) is fully implemented for both `InMemoryStore` and
`PostgresStore`, but the automated tests exercise the InMemory path. Before relying on a Postgres-backed
deploy, run one **AgentInstance round-trip smoke** against a real `DATABASE_URL`:

1. Point `DATABASE_URL` at the target Postgres and run the app's DB init (the `agent_instances` table +
   status `CHECK` constraint are created idempotently alongside the other tables).
2. Deploy a **replay/paper** agent (Studio or `POST /agents/deploy`) — this exercises the honest
   default path, no real money.
3. Confirm the round-trip: after the deploy returns a `run_id`, load the instance back
   (`store.get_agent_instance(instance_id)`) and verify the pinned fields survive
   (`config_hash`, `policy_hash`, `run_id`, `status`, `preflight_checks`), and that after the run seals
   the stored `status` advanced to `sealed`. This is the one path the InMemory tests can't prove for the
   SQL backend.
4. Confirm the deployed run verifies through the same `/runs/{id}/verify` path as an arena run.

If the round-trip works, Postgres deploy persistence is trustworthy for production.

---

## Part B — the 1-share live_guarded FAK smoke (real money)

This is the gated, real-money path. Do it once, with 1 share, and treat it as the acceptance test for
"live_guarded actually works."

### B0. Prerequisites (wallet + approvals)
- Fund the trading wallet with **USDC**.
- Grant **USDC** and **CTF (ERC-1155)** approvals to **both** the regular exchange **and** the neg-risk
  exchange. (The neg-risk exchange is a separate contract; missing its approval is a common failure
  mode.)

### B1. Run the 1-share FAK smoke
- Run `scripts/polymarket_smoke.py`, gated by the env vars it requires: **`POLYMARKET_SMOKE=yes`** and
  **`POLYMARKET_WRITE_ENABLED`** (the script refuses to place a real order unless both are set — this is
  deliberate friction).
- It places a **single 1-share Fill-Or-Kill** order to prove the write path end-to-end on mainnet.
- **Acceptance criterion (important):** the smoke passes **only** if you get a *real* `LIVE_GUARDED`
  submit/receipt. A run that comes back `dry_run` means the path was **not armed** — treat that as a
  *failed* smoke, not a pass. (A degraded dry run is safe, but it is not proof the live path works.)

### B2. Verify the on-chain neg-risk approval
- Independently confirm the neg-risk exchange's ERC-1155 approval is set on-chain. This is **not
  offline-verifiable** by the preflight — it must be operator-confirmed.

### B3. Set `live_ready` (only after B1 + B2 both pass)
`run_preflight(...)` in `veridex/venues/polymarket_preflight.py` takes two **operator-verify** tri-state
params that default to `None` (unverified) and are **never auto-run**:
- `neg_risk_approved=True` — your confirmation of B2.
- `fak_smoke_passed=True` — your confirmation of B1.

`live_ready` becomes `True` **only** when `PreflightReport.ok` is True **and both** of these are
explicitly `True`. `ok=True` alone does **not** arm live — the two operator checks are mandatory.

### B4. Build the armed adapter
- Resolve the market: `resolve_market(...)` → a `ResolvedMarket` (handles 1X2 per-team / draw-as-YES /
  O/U multi-slug; raises `MarketUnavailable` on any ambiguity rather than guessing a token).
- Construct the write-enabled adapter: `PolymarketAdapter(resolved=..., side=..., write_client=...,
  dry_run=False, ...)` with `POLYMARKET_WRITE_ENABLED=true`. This adapter carries the real-venue marker
  (so `real_venue_quote` is earned) and the `_require_armed` lock.

### B5. Set the live safety envelope
On the `PolicyEnvelope` (`veridex/policy/envelope.py`):
- **`max_stake_live_guarded`** — the tighter per-order cap that applies **only** on the live path. Set
  this small (e.g. 1 share) for the smoke.
- **`circuit_breaker_threshold`** — consecutive executed failures that trip the breaker (blocks the rest
  of the run, fail-closed).
- **`cooldown_s`** — minimum seconds between consecutive orders.

### B6. Wire and run
- `LiveExecutionDeps(adapter=<armed>, live_ready=report.live_ready)` →
  `start_competition(..., live_deps=<that bundle>)`.
- Note: the HTTP `POST /competitions/{id}/start` endpoint passes **no** `live_deps` by design, so a
  real order **cannot** be armed over the API. Real money is reachable **only** by an operator directly
  supplying the armed `LiveExecutionDeps` — this is intentional (real money is operator-direct-only,
  never over HTTP or by an automated agent).

---

## What protects you at run time (fail-closed guarantees)

- **Two independent locks** — the route arm gate and the adapter `_require_armed`; both must pass.
- **Structural mode gate** — anything other than `LIVE_GUARDED` degrades to dry, by construction.
- **Circuit breaker** — an OPEN breaker denies the submit with zero venue I/O; only *executed* failures
  trip it (policy denials and dry fills do not).
- **Live cap** — `max_stake_live_guarded` denies an over-cap order pre-quote.
- **Quote-size coupling** — the quoted edge is priced for the exact size that submits.
- **Resolver never guesses** — an ambiguous market/side raises `MarketUnavailable`, never a wrong token
  (the `1X2|away` bet maps to the away-*wins* token; draw maps to YES on the draw-binary market).
- **Honest degrade** — if the run is configured live but not armed, it degrades to dry and records
  `degraded_because_not_armed` with the reason (`live_ready_false` | `missing_live_deps` |
  `non_real_adapter`) — so you can always tell *why* a run went dry.

## Abort
- To stop arming, unset `POLYMARKET_WRITE_ENABLED` / pass `dry_run=True` / omit `live_deps` — any one of
  these returns the run to a dry-run simulation with no real orders.
- The circuit breaker + cap bound the blast radius even if a run is armed.
