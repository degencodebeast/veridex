"""R4-A admission contracts (Section 4.2): the pinned strategy experiment manifest and the
deterministic authorization decision.

``StrategyExperimentManifest`` is frozen and pinned like ``DeployConfig``/``AgentInstance``:
its ``manifest_hash()`` = ``sha256(serialize_payload(model_dump()))`` binds the whole content,
INCLUDING the explicit ``execution_wallet_binding_hash`` field (``None`` in Mode A, equal to
``ExecutionWalletBinding.binding_hash()`` in Mode B — v0.6.1, REQ-018/AC-042). Because the
wallet/policy pin lives inside ``manifest_hash()`` itself, it can never be a checked-separately
sidecar that is omitted after restart.

``StrategyAuthorizationDecision`` binds ``manifest_hash`` + ``policy_hash`` + session state to a
deterministic ``ALLOW``/``DENY`` verdict BEFORE any execution attempt — identical inputs yield an
identical result (AC-021). A missing manifest blocks execution (Section 6 group 12); an
``EXPERIMENTAL_DUST`` manifest admits WITHOUT a profitability flag but still trips the loss caps.

This module imports only ``.contracts`` (same isolated package) and the standard library — no
``veridex.live_recorder`` and no ranked-lane dependency (SEC-003).
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import field_validator

from veridex.dust_execution.contracts import (
    EvidenceClass,
    ExecutionMode,
    _FrozenModel,
)
from veridex.runtime.evidence import serialize_payload

AdmissionVerdict = Literal["ALLOW", "DENY"]


class StrategyExperimentManifest(_FrozenModel):
    """A pinned, frozen strategy experiment manifest (Section 4.2).

    ``execution_wallet_binding_hash`` is an EXPLICIT frozen field: ``None`` in Mode A, and in
    Mode B it equals ``ExecutionWalletBinding.binding_hash()`` (declared by a later task) so the
    wallet/policy pin is INSIDE ``manifest_hash()``. Admission and restart recompute it from the
    live binding and compare; any mismatch fails closed (enforced by later tasks — this contract
    only declares the frozen field).
    """

    strategy_id: str
    strategy_config_hash: str
    evidence_class: EvidenceClass
    market: str
    universe: tuple[str, ...]
    mode: ExecutionMode
    max_orders: int
    max_notional: float
    max_session_loss: float
    max_daily_loss: float
    session_window: tuple[int, int]  # (start_ms, end_ms)
    required_inputs: tuple[str, ...]
    permitted_intent_kinds: tuple[str, ...]
    market_fee_snapshot_hash: str
    execution_wallet_binding_hash: str | None = None  # None in Mode A (v0.6.1, REQ-018/AC-042)
    operator_authorization: str
    forbidden_claims: tuple[str, ...]

    @field_validator("universe")
    @classmethod
    def _universe_non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("universe must name at least one token/market")
        return value

    def manifest_hash(self) -> str:
        """``sha256`` over ``serialize_payload(model_dump())`` (canonical, deterministic).

        Pinned only after the named fail-closed preflight checks pass (enforced by later tasks).
        Equal to :meth:`_FrozenModel.config_hash` but named per the Section 4.2 contract.
        """
        canonical = serialize_payload(self.model_dump())
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SessionState(_FrozenModel):
    """The deterministic session state consumed by admission (Section 4.2 ``session``).

    Fee-inclusive realized-loss accumulators plus breaker/kill-switch flags. Frozen so an
    authorization decision is a pure function of ``(manifest, policy_hash, session)``.
    """

    session_id: str
    realized_loss_session: float
    realized_loss_daily: float
    open_order_count: int
    breaker_open: bool
    kill_switch_engaged: bool


class StrategyAuthorizationDecision(_FrozenModel):
    """A deterministic admission verdict (Section 4.2).

    Built ONLY by :meth:`evaluate` so the verdict + ordered reason codes are a pure function of
    the admission inputs — identical ``(manifest_hash, policy_hash, session)`` → identical result
    (AC-021). ``manifest_hash`` is ``None`` when no manifest was supplied.
    """

    verdict: AdmissionVerdict
    reason_codes: tuple[str, ...]
    manifest_hash: str | None
    policy_hash: str
    session_id: str

    @classmethod
    def evaluate(
        cls,
        *,
        manifest: StrategyExperimentManifest | None,
        policy_hash: str,
        session: SessionState,
    ) -> StrategyAuthorizationDecision:
        """Deterministically admit or deny execution BEFORE any submit attempt.

        Fail-closed, order-stable checks (Section 6 group 12):

        * no manifest → ``DENY`` (``missing_manifest``) — never admit without a pinned manifest;
        * kill switch engaged → ``DENY`` (``kill_switch_engaged``);
        * breaker open → ``DENY`` (``breaker_open``);
        * a positive session-loss cap already met/exceeded → ``DENY`` (``session_loss_cap``);
        * a positive daily-loss cap already met/exceeded → ``DENY`` (``daily_loss_cap``);
        * otherwise ``ALLOW`` — an ``EXPERIMENTAL_DUST`` manifest admits WITHOUT a profitability
          flag, at the strictest caps (REQ-010/AC-024).

        Reason codes are appended in this fixed order, so identical inputs yield an identical
        (byte-stable) decision.
        """
        if manifest is None:
            return cls(
                verdict="DENY",
                reason_codes=("missing_manifest",),
                manifest_hash=None,
                policy_hash=policy_hash,
                session_id=session.session_id,
            )

        reason_codes: list[str] = []
        if session.kill_switch_engaged:
            reason_codes.append("kill_switch_engaged")
        if session.breaker_open:
            reason_codes.append("breaker_open")
        # Loss caps are magnitudes; a cap <= 0 is disabled (mirrors max_stake_live_guarded).
        if manifest.max_session_loss > 0.0 and session.realized_loss_session >= manifest.max_session_loss:
            reason_codes.append("session_loss_cap")
        if manifest.max_daily_loss > 0.0 and session.realized_loss_daily >= manifest.max_daily_loss:
            reason_codes.append("daily_loss_cap")

        verdict: AdmissionVerdict = "DENY" if reason_codes else "ALLOW"
        return cls(
            verdict=verdict,
            reason_codes=tuple(reason_codes),
            manifest_hash=manifest.manifest_hash(),
            policy_hash=policy_hash,
            session_id=session.session_id,
        )
