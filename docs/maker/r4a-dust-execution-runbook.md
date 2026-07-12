# R4-A Dust-Execution Operator Runbook

**Status:** `EXPERIMENTAL_DUST` · `NOT_PROVEN_EDGE` · Mode B **UNARMED / offline** by default.

This runbook is the operator's guide to the R4-A dust-execution lane
(`veridex/dust_execution/`): the REAL-fill, tiny-size ("dust") execution path that
exercises the money-network safety machinery end to end. It documents the Mode A → Mode B
progression, the operator preconditions, the CLOB-V2 compatibility gate, the
emergency-stop / re-arm operations, and the Gate-B-parallel posture.

> **Honesty first (SEC-001, CON-003).** R4-A ships **safety-complete**, not
> **alpha-proven**. Every dust run is labelled `DUST_LIVE` / `EXPERIMENTAL_DUST` /
> `UNCALIBRATED` / `NOT_PROVEN_EDGE`. A dust run that completes without tripping a safety
> stop is a **scoped-negative** finding — it proved the *safety envelope holds*, NOT that a
> strategy has edge. R4-A **never claims proven alpha** and never promotes a finding. Only
> **Gate B** (out of R4-A scope — see [§9](#9-gate-b-parallel-non-blocking-posture)) controls
> promotion, judge-facing claims, and scaling.

---

## 1. The mandatory honesty labels (AC-025)

Every dust run emits a terminal `DustRunLabelEvent`
(`veridex/dust_execution/contracts.py`) carrying four **mandatory, pinned** labels:

| Label field         | Pinned value        | Meaning |
|---------------------|---------------------|---------|
| `run_label`         | `DUST_LIVE`         | This is a live-wire dust run (real fills, tiny size). |
| `evidence_class`    | `EXPERIMENTAL_DUST` | A defined-but-unvalidated strategy; admits without a profitability flag. |
| `calibration_label` | `UNCALIBRATED`      | No calibration has been established. |
| `edge_label`        | `NOT_PROVEN_EDGE`   | No edge is claimed or implied. |

The values are `Literal`-pinned on the contract, so a **softened** label is
*unconstructable* — a run cannot emit a label that reads "calibrated" or "proven".
`veridex/dust_execution/analysis.py::assert_mandatory_dust_run_labels` is the
defense-in-depth checker that rejects any label-like object which downgrades a mandatory
value before it can narrate a run as calibrated / proven / non-live.

There is deliberately **no** `expected_pnl` / `edge_bps` field on the run result — the
result structurally implies no profitability or edge claim.

## 2. Diagnostic post-trade markout (AC-014)

Post-trade markout is computed by
`veridex/dust_execution/analysis.py::compute_markout`. It is **diagnostic-only**:

- **Keyed by `decision_id`.** The markout is derived from a sealed `OwnFillEvent` and
  carries the same `decision_id` join key.
- **Non-mutating.** It *reads* the sealed fill and returns a **new**
  `PostTradeMarkoutEvent`; it never writes back to the sealed order/decision event (which is
  a frozen contract regardless). The source event is byte-identical before and after.
- **Non-rankable.** Its output fields (`reference_price`, `markout_bps`) are on the
  SEC-006 rank denylist (`veridex/rank_guards.py::R4A_EXECUTION_DENYLIST_FIELDS`), so a
  markout can **never** reach the ranked lanes (`veridex.scoring`, `veridex.leaderboard`,
  `veridex.maker.leaderboard`). Any attempt to feed a markout field to a rank surface raises.

Markout is a post-hoc *diagnostic* of price movement relative to a fill — it is **not** a
scored, ranked, or promotable signal.

## 3. Scoped-negative findings are not relabellable (SEC-001, CON-003)

A **scoped-negative finding** — a dust run that proved *safety*, not *alpha* — has its
evidence class **structurally pinned** to `EXPERIMENTAL_DUST` by
`analysis.py::label_for_scoped_negative`: there is no parameter that can set it to anything
else, so no request field, label input, LLM output, or metadata blob can relabel the
finding. An **explicit** promotion request (`EVIDENCE_GATED` / `PROMOTED`) is additionally
**refused fail-closed** by `analysis.py::reject_scoped_negative_relabel`. Promotion is a
Gate B concern, out of R4-A scope.

---

## 4. Mode A → Mode B progression

R4-A runs in one of two execution modes (`ExecutionMode` in `contracts.py`):

- **Mode A — `dry_run`.** Fake/dry-run adapter; no real money. Mode A and Mode B emit the
  **identical event types in the identical order**, so Mode A fully exercises the lifecycle
  and safety machinery offline.
- **Mode B — `live_guarded`.** Real-money, real-fill, fully guarded.

**Mode A is the HARD GATE.** `mode_a_passed` must be true before Mode B can arm — even if
every other precondition is green (`runner.py::_mode_b_arming_block_reason`). The progression
is strictly Mode A → Mode B, never the reverse and never a skip.

Mode B is **UNARMED / offline by default**. A bare CLI invocation drives the facade with
`arming=None`, so Mode B stays unarmed and no live venue / Privy / provisioning call is ever
made.

## 5. Operator preconditions

### 5.1 The human interlock (E7-T3)

Mode B requires an **explicit human precondition** to be satisfied, recorded as an
`OperatorInterlockEvent` (`contracts.py`): one satisfied `precondition`, a non-secret
`operator_authorization_ref`, and an explicit `first_order_authorized` flag. Arming is
**never implicit** — the CLI requires an explicit `--i-am-operator` (AC-023), and a bare
invocation cannot reach a live venue.

### 5.2 The full Mode-B arming bundle (E6-T4)

Mode B arms ONLY when **every** member of `ModeBArming` (`runner.py`) positively passes —
fail-closed AND, evaluated in a fixed order so the check cannot be partially satisfied:

1. `mode_a_passed` — the Mode A → B **hard gate** (§4).
2. `clobv2_gate.mode_b_admitted` — the E3-T5 CLOB-V2 write-contract gate (see [§6](#6-clob-v2-compatibility-gate-e3-t5)).
3. `privy_preflight.ok is True` — the E3-T8 operator-run Privy signing preflight.
4. `provisioning.ok is True` — the E3-T8 operator-run pUSD / approvals / gas provisioning.
5. `binding` + `live_policy` + `live_quorum` — a valid `ExecutionWalletBinding` whose
   `binding_hash` verifies against the pinned manifest AND whose policy-content-hash + quorum
   verify against the live policy/quorum. Any mismatch or weakening fails closed.

The operator-supplied tri-states (`clobv2_gate`, `privy_preflight`, `provisioning`) are
`ok=None` until an operator runs them **out of CI**. Offline tests drive each pass/fail with
a genuine fixture — Mode B stays UNARMED and no live call is made. Removing any branch would
let Mode B arm when it must not; each is guarded by a named test.

## 6. CLOB-V2 compatibility gate (E3-T5)

The CLOB-V2 write-contract gate lives in `veridex/dust_execution/clobv2_gate.py`. It gates
Mode B on two independent conditions, surfaced by `Clobv2GateResult`:

- **`machine_ok`** — the machine fixture-match: signed-order, send-order, cancel-response,
  and get-orders-page fixtures validate against the CLOB-V2 write contract
  (`validate_signed_order`, `validate_sendorder_fixture`, `validate_cancel_response`,
  `validate_get_orders_page`).
- **operator production smoke** — `operator_production_smoke` / `ProductionSmokeResult`: an
  operator-confirmed production smoke run (`operator_smoke_ok is True`), performed OUT of CI.

`mode_b_admitted` is true **only** when both hold. `evaluate_clobv2_gate` /
`evaluate_from_fixture_dir` compose the verdict from fixtures. Until an operator runs the
production smoke, the gate is not admitted and Mode B cannot arm.

## 7. Emergency-stop and re-arm operations

### 7.1 Emergency stop — `cancel_all_and_block` (E2, SAF-002/003)

The `SafetyController.cancel_all_and_block` primitive (`veridex/dust_execution/emergency.py`)
is the **single** idempotent emergency stop. On the first call for a session it **blocks new
submits first, THEN sweeps** all resting orders via the venue `cancel_all_orders`
(`DELETE /cancel-all`) seam. It carries only a trigger *cause* (`CancelAllCause`), never a
single order id (SAF-003). A subsequent call is a **no-op**: it does not re-fire the wire and
KEEPS the block set. There is deliberately no per-order cancel path on the emergency lane.

Trigger causes (`CancelAllCause`): `breaker`, `kill_switch`, `shutdown`, `manual`,
`loss_breach` (realized-loss-cap breach), `reconciliation_timeout` (automated
reconciliation-timeout fallback). Each carries its own cause so audit fidelity is preserved.

### 7.2 Shutdown (E6-T6, SAF-006)

A shutdown MUST resolve to exactly one of two explicit outcomes: sweep resting orders, or an
explicit, **recorded** choice to leave them resting. A silent abandon is not a representable
state.

### 7.3 Re-arm — `re_arm` (SAF-004): a SEPARATE, fail-closed operator action

Clearing the emergency stop is the **dangerous** action, so it is structurally distinct from
every stop trigger: **a stop can never re-arm as a side effect**, and a duplicate engage is
never inferred as a re-arm. `SafetyController.re_arm` is the ONLY path that clears
`submit_blocked`, and it clears it ONLY when all **three** preconditions are positively
satisfied (evaluated auth → risk → reconciliation):

1. **Explicit operator authorization** (`operator_authorized=True`) — a deliberate operator
   act, not an implicit retry.
2. **Risk-state reload (SAF-002c)** — a fresh accumulator rebuilt from the durable ledger
   via `ledger.reconstruct_risk` MUST be supplied, so the loss caps that fail-close Mode B
   are re-established before trading resumes. A `None` risk fails closed.
3. **Open-order reconciliation** — the venue must report **zero** resting orders; any
   non-zero count means capital is still exposed, so re-arming is refused.

Any unsatisfied precondition raises `ReArmDenied` and leaves the stop **ENGAGED**.
Fail-closed is the whole point.

## 8. Operator quick reference (CLI)

The operator-only CLI is `scripts/maker/dust_execution.py` (E7-T4, SEC-005):

- A bare / `--dry-run` invocation prints status (`configured: <bool>`, the selected signer
  mode, `mode_b_armed: false`) and **never** a secret (SEC-005). Mode B stays UNARMED.
- Arming is never implicit: the operator must pass `--i-am-operator` (AC-023).
- It **fails closed** on a missing signer / precondition and drives the facade with
  `arming=None` — a bare invocation cannot reach a live venue.

## 9. Gate-B-parallel / non-blocking posture

R4-A ships **safety-complete** and runs **in parallel with, and non-blocking on, Gate B**:

- **R4-A owns:** the money-network safety envelope — isolation boundaries, fail-closed
  arming, the emergency stop / re-arm, honest labels, and the diagnostic (non-rankable)
  markout. Mode B stays UNARMED/offline until an operator explicitly arms it out of CI.
- **Gate B owns (out of R4-A scope):** promotion (`EVIDENCE_GATED` / `PROMOTED`),
  judge-facing claims, and scaling. R4-A never relabels a scoped-negative finding, never
  claims proven alpha, and never promotes.

R4-A does not wait on Gate B to be safety-complete, and Gate B cannot reach back through
R4-A to relabel or promote a finding. The boundary is one-way and structural.

---

### Cross-references

- Contracts: `veridex/dust_execution/contracts.py`
- Diagnostic markout + honesty labels: `veridex/dust_execution/analysis.py`
- Rank denylist (SEC-006): `veridex/rank_guards.py`
- Emergency stop / re-arm: `veridex/dust_execution/emergency.py`
- Mode-B arming: `veridex/dust_execution/runner.py`
- CLOB-V2 gate: `veridex/dust_execution/clobv2_gate.py`
- Operator CLI: `scripts/maker/dust_execution.py`
- Isolation / honesty tests: `tests/test_dust_execution_sec_isolation.py`
