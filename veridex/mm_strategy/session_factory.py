"""II-5 — the quoteguard-mm session factory: an AUTHORITY RECONSTRUCTOR, never a request mapper.

Reconstructs a `run_market_maker` session bundle ONLY from server-owned state (the persisted
`AgentInstance` + the server-pre-allocated `RunContext`). Per Codex's II-5 ruling (requirement 3),
EVERY request/body value is non-authoritative: the factory re-derives `StrategyConfig` / the maker
`PolicyEnvelope` / the `StrategyExperimentManifest` from the PERSISTED, bounded MM config and asserts
hash equality against what preflight pinned — a forged/corrupted value fails CLOSED before the
adapter or receipt proposer ever runs. The 9-step chain is documented on
:func:`reconstruct_mm_session`.

The tape and the dry-run proposer arrive through an INJECTABLE, SERVER-SIDE seam (mirrors
`veridex.api.deploy.DeployDeps`) — never a request field. The production tape catalog
(`MM_TAPE_CATALOG`) banks exactly ONE entry — `txline-mm-18213979-v1`, the `fu-ii5-demo-tape`
follow-up — a HYBRID tape of VERBATIM recorded SX Bet in-play order-book + TxLINE fair-value rows (FIFA
World Cup Norway v England, fixture 18213979, ~51' in-play) with deterministically-set orchestration
scaffolding (see
`veridex.mm_strategy.demo_tape` for the full field-provenance table). Its content hash is re-verified at
resolve time; it is SELF-WARMING (the deploy's default cold seed folds its real warmup prefix — no
injected seed). Every OTHER `tape_ref` still fails closed: `default_mm_tape_resolver` raises a clear,
honest error naming the missing catalog entry — it NEVER fabricates a tape. Tests / operators may still
inject a real tape through `DeployDeps.mm_tape_resolver`.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from veridex.deploy.preflight import DeployConfig, MakerDeployConfig
from veridex.dust_execution.manifest import StrategyExperimentManifest
from veridex.live_recorder.contracts import LiveRecorderSessionMeta
from veridex.mm_strategy import demo_tape
from veridex.mm_strategy.composition import MakerInstanceConfig
from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import StrategyState
from veridex.mm_strategy.execution_adapter import R4ARequestConfig
from veridex.mm_strategy.offline_proposer import OfflineRecordingProposer
from veridex.mm_strategy.orchestration import FacadeDeps
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.evidence import serialize_payload

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from veridex.deploy.instance import AgentInstance
    from veridex.dust_execution.facade import MMExecutionToolResult
    from veridex.runtime.mm_agent_adapter import RunContext

logger = logging.getLogger(__name__)

#: The strategy family discriminator (req 5 — dispatch is FAMILY-driven, never template/archetype).
MM_STRATEGY_FAMILY = "quoteguard-mm"

#: Pinned request-config sizing inputs (REQ-058 — session/manifest-pinned, never agent-supplied).
_MM_WALLET_EQUITY_AT_DECISION = 1000.0
_MM_FIXED_FRACTION = 0.001

#: The explicit II-5 mode matrix (Codex ruling requirement 4). Any pair not present here is REJECTED.
_MODE_MATRIX: dict[tuple[str, str], str] = {
    ("replay", "paper"): "replay",
    ("replay", "dry_run"): "replay_dry_run",
}


class MMAuthorityMismatchError(ValueError):
    """A reconstructed value disagrees with a server-pinned authority — fails CLOSED before any run."""


class MMModeRejectedError(ValueError):
    """The (source_mode, execution_mode) pair is not in the explicit mode matrix — fails CLOSED."""


class MMTapeNotFoundError(LookupError):
    """No catalog entry for a `tape_ref` — an honest, fail-closed "not yet banked" signal (A8)."""


def derive_mm_mode(source_mode: str, execution_mode: str) -> str | None:
    """Return the composition `mode` string for `(source_mode, execution_mode)`, or `None` if rejected.

    The explicit matrix (Codex req 4): `replay+paper -> "replay"` (OPS, no receipt required);
    `replay+dry_run -> "replay_dry_run"` (OPS + dry-run receipt); every `live_guarded` pair and every
    `live` pair is rejected (`None`) — II-5 provisions no reviewed live-tape composition.
    """
    return _MODE_MATRIX.get((source_mode, execution_mode))


# ---------------------------------------------------------------------------
# The replay tape carrier + content-hash verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MakerReplayTape:
    """A content-hash-verifiable maker replay tape (the `tape` `run_market_maker` folds).

    Attributes:
        tape_ref: The catalog key this tape was resolved from (the server-owned reference).
        identity: The tape's stream identity (fixture/market/side/token).
        venue_market_ref: The venue-native market reference the tape's ticks quote against.
        events: The ordered cadence events (`FvArrival` / `ObservationTick`) `run_market_maker` folds.
        content_hash: The pinned content hash of `events` (verified against a fresh recomputation at
            resolve time — never trusted from an arbitrary path/fixture/request object).
    """

    tape_ref: str
    identity: Any
    venue_market_ref: str
    events: tuple[Any, ...]
    content_hash: str


def _to_jsonable(value: Any) -> Any:
    """Recursively lower dataclasses / pydantic models / containers to JSON-primitive structures.

    `FvArrival` / `ObservationTick` are frozen dataclasses that nest pydantic `StreamIdentity` /
    `AssemblerOwnedFacts` values; `ObservationTick.build` is a non-serializable closure and is
    excluded (it carries no tape IDENTITY — only the tick's declared facts + the cadence-assigned
    sequence/guard leg it is invoked with are part of the tape's content).
    """
    import dataclasses

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _to_jsonable(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if f.name != "build"
        }
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump())
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def compute_tape_content_hash(events: tuple[Any, ...]) -> str:
    """The deterministic content hash of a tape's ordered events (the SAME canonical serializer)."""
    rows = [_to_jsonable(event) for event in events]
    return hashlib.sha256(serialize_payload(rows).encode("utf-8")).hexdigest()


#: The production tape catalog. Two distinct, immutable keys with visible provenance:
#:  - `txline-mm-18213979-v1` — the parked `fu-ii5-demo-tape` SX HYBRID (real recorded SX book +
#:    TxLINE fair-value rows + derived session metadata — see `veridex.mm_strategy.demo_tape`).
#:  - `pmxt-txline-mm-18209181-v1` — the provenance-correct REAL-DATA tape (REAL Polymarket 10-level
#:    depth + REAL TxLINE 1X2 fair value, France v Morocco fixture 18209181 — see
#:    `veridex.mm_strategy.pmxt_tape`), added by `_register_pmxt_txline_tape()` below. This is the
#:    Studio demo path.
#: Each tape's content hash is re-verified at resolve time (step 7 of `reconstruct_mm_session`). Any
#: other key fails closed.
MM_TAPE_CATALOG: dict[str, Callable[[], MakerReplayTape]] = {
    demo_tape.TAPE_REF: demo_tape.build_txline_mm_tape,
}


def _register_pmxt_txline_tape() -> None:
    """Bank the real-data `pmxt-txline-mm-18209181-v1` tape (lazy import avoids a load-time cycle:
    `pmxt_tape` imports this module's `MakerReplayTape` / `compute_tape_content_hash`)."""
    from veridex.mm_strategy import pmxt_tape

    MM_TAPE_CATALOG[pmxt_tape.TAPE_REF] = pmxt_tape.build_pmxt_txline_tape


_register_pmxt_txline_tape()


def default_mm_tape_resolver(tape_ref: str) -> MakerReplayTape:
    """Resolve `tape_ref` against the production catalog — fails CLOSED, never fabricates a tape.

    Raises:
        MMTapeNotFoundError: `tape_ref` has no catalog entry (today: always, until `fu-ii5-demo-tape`
            bank a verified production tape).
    """
    factory = MM_TAPE_CATALOG.get(tape_ref)
    if factory is None:
        raise MMTapeNotFoundError(
            f"no production replay tape banked for tape_ref={tape_ref!r} "
            "(fu-ii5-demo-tape is the tracked follow-up; inject DeployDeps.mm_tape_resolver to supply one)"
        )
    return factory()


# ---------------------------------------------------------------------------
# Offline wire sentinel + best-effort freeze sink (production-safe defaults)
# ---------------------------------------------------------------------------


class _OfflineWireSentinel:
    """ANY attribute access raises — proof the dry-run proposer never touches a wire primitive.

    Bound as `FacadeDeps.adapter` / `.signer` / `.sources`: the offline proposer receives these as
    opaque values (via `bound()`) and never calls into them, so the run completes without ever
    tripping `__getattr__`.
    """

    def __init__(self, label: str) -> None:
        self._label = label

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"wire primitive touched on {object.__getattribute__(self, '_label')!r}: {name!r}")


class _LoggingFreezeSink:
    """The session-owned durable freeze sink — logs every `FreezeRecord` (best-effort, never raises)."""

    def emit(self, record: Any) -> None:
        logger.warning("mm session freeze: %r", record)


async def _no_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# Typed, normalized MM effective config (parsed with unknown fields FORBIDDEN)
# ---------------------------------------------------------------------------


def _session_dir_for(instance_id: str, run_id: str) -> Path:
    """The production per-run session directory (a durable recorder sink)."""
    path = Path(tempfile.gettempdir()) / "veridex-mm-sessions" / instance_id / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_maker_strategy_config(mm: MakerDeployConfig) -> StrategyConfig:
    """Reconstruct the pinned, hash-bound `StrategyConfig` from the persisted MM config (step 4)."""
    return StrategyConfig(guard_enabled=mm.guard_enabled, tif=mm.tif)  # type: ignore[arg-type]


def build_maker_policy_envelope(config: DeployConfig, mm: MakerDeployConfig) -> PolicyEnvelope:
    """The MAKER-specific admitted `PolicyEnvelope` — NEVER `DeployConfig.to_policy_envelope()`.

    Distinct from the directional single-order envelope: it admits the MULTI-order maker quote plan
    (`max_orders_per_run/session/day` from the bounded MM config) plus the maker loss caps, while
    staying bounded and hash-consistent (step 5).
    """
    return PolicyEnvelope(
        max_stake=config.max_stake,
        max_orders_per_run=mm.max_orders_per_run,
        max_orders_per_session=mm.max_orders_per_session,
        max_orders_per_day=mm.max_orders_per_day,
        venue_allowlist=list(config.venue_allowlist),
        market_allowlist=list(config.market_allowlist),
        min_edge_bps=config.min_edge_bps,
        max_slippage_bps=100,
        max_price=1000.0,
        max_quote_age_s=60,
        cooldown_s=0,
        human_approval_threshold=config.max_stake + 1.0,
        max_session_loss=mm.max_session_loss,
        max_daily_loss=mm.max_daily_loss,
        kill_switch=False,
    )


def build_maker_manifest(
    config: DeployConfig,
    mm: MakerDeployConfig,
    *,
    operator_id: str,
    strategy_config_hash: str,
) -> StrategyExperimentManifest:
    """Reconstruct the pinned `StrategyExperimentManifest`, bound to `strategy_config_hash` (step 4/5)."""
    now_ms = int(time.time() * 1000)
    duration_ms = max(config.duration_s or 3600, 60) * 1000
    fee_snapshot = hashlib.sha256(",".join(sorted(config.venue_allowlist)).encode("utf-8")).hexdigest()
    return StrategyExperimentManifest(
        strategy_id="venue-anchored-txline-guarded-maker",
        strategy_config_hash=strategy_config_hash,
        evidence_class="EXPERIMENTAL_DUST",
        market=config.market_allowlist[0],
        universe=tuple(config.market_allowlist),
        mode="dry_run",
        max_orders=mm.max_orders_per_run,
        max_notional=config.max_stake,
        max_session_loss=mm.max_session_loss,
        max_daily_loss=mm.max_daily_loss,
        session_window=(now_ms, now_ms + duration_ms),
        required_inputs=("fair_value", "venue_book"),
        permitted_intent_kinds=("make_quote", "cancel_replace", "cancel_all", "no_quote"),
        market_fee_snapshot_hash=fee_snapshot,
        operator_authorization=operator_id,
        forbidden_claims=("PROVEN_EDGE", "CALIBRATED"),
    )


def _session_meta(strategy_config: StrategyConfig, fixture_id: int) -> LiveRecorderSessionMeta:
    return LiveRecorderSessionMeta(
        session_ts=int(time.time()),
        endpoints={"venue": "offline://quoteguard-mm"},
        tool_version="ii5-session-factory",
        config_hash=strategy_config.config_hash(),
        source_provenance={"venue": "offline"},
        fixture_ids=(fixture_id,),
    )


# ---------------------------------------------------------------------------
# The 9-step authority reconstruction
# ---------------------------------------------------------------------------


def reconstruct_mm_session(
    instance: AgentInstance,
    ctx: RunContext,
    *,
    tape_resolver: Callable[[str], MakerReplayTape] | None = None,
    proposer: Callable[..., Awaitable[MMExecutionToolResult]] | None = None,
    seed_state: StrategyState | None = None,
    session_dir: Path | None = None,
) -> tuple[MakerInstanceConfig, MakerReplayTape, str, bool]:
    """Reconstruct the `run_market_maker` session bundle ONLY from server-owned state.

    The 9-step chain (Codex II-5 ruling, requirement 3):

    1. Load `instance` (caller-supplied, already the PERSISTED record) + verify `RunContext` identity
       coherence (`ctx.run_id == instance.run_id`, `ctx.owner_did == instance.operator_id`).
    2. Parse the typed, normalized MM config (`MakerDeployConfig`, `extra="forbid"`) off
       `instance.effective_config` — unknown fields FORBIDDEN.
    3. Recompute the FULL submitted deploy config hash (`DeployConfig.config_hash()` reconstructed
       from `instance.submitted_config`) and require equality with `instance.config_hash`.
    4. Reconstruct `StrategyConfig`; recompute `strategy_config_hash`.
    5. Reconstruct the maker `PolicyEnvelope`; recompute `policy_hash` and require equality with
       `instance.policy_hash`; bind `strategy_config_hash` into the reconstructed manifest.
    6. Bind the SAME `manifest` + `envelope` objects BY IDENTITY into `MakerInstanceConfig` and
       `FacadeDeps` (preserves `run_market_maker`'s `is` identity asserts — the II-1 contract).
    7. Load the tape via the injected (server-side) resolver, keyed by the persisted `tape_ref` —
       NEVER an arbitrary path/fixture/request object — and verify its content hash.
    8. Derive `guard_enabled` from the hash-bound `StrategyConfig`.
    9. Derive `mode` from the explicit matrix (:func:`derive_mm_mode`).

    Raises:
        MMAuthorityMismatchError: Any identity/hash cross-check fails (fail-closed, before the
            adapter driver or receipt proposer runs).
        MMModeRejectedError: `(instance.source_mode, instance.execution_mode)` is not permitted.
        MMTapeNotFoundError: The resolved `tape_ref` has no banked tape.
    """
    # (1) Runtime/owner coherence — the RunContext identity must match the persisted instance.
    if ctx.run_id != instance.run_id:
        raise MMAuthorityMismatchError(
            f"run_id mismatch: RunContext={ctx.run_id!r} instance.run_id={instance.run_id!r}"
        )
    if ctx.owner_did != instance.operator_id:
        raise MMAuthorityMismatchError(
            f"owner mismatch: RunContext.owner_did={ctx.owner_did!r} "
            f"instance.operator_id={instance.operator_id!r}"
        )

    # (9, checked early — fail closed before any reconstruction work) mode matrix.
    mode = derive_mm_mode(instance.source_mode, instance.execution_mode)
    if mode is None:
        raise MMModeRejectedError(
            f"quoteguard-mm: (source_mode={instance.source_mode!r}, "
            f"execution_mode={instance.execution_mode!r}) is not a permitted replay/dry-run pair"
        )

    # (2) Typed, normalized MM config — unknown fields FORBIDDEN.
    mm = MakerDeployConfig.model_validate(instance.effective_config)

    # (3) Recompute the FULL deploy config hash from the persisted submitted config.
    submitted = DeployConfig.model_validate(instance.submitted_config)
    recomputed_config_hash = submitted.config_hash()
    if recomputed_config_hash != instance.config_hash:
        raise MMAuthorityMismatchError(
            f"config_hash mismatch: recomputed={recomputed_config_hash!r} "
            f"instance.config_hash={instance.config_hash!r}"
        )

    # (4) StrategyConfig + strategy_config_hash.
    strategy_config = build_maker_strategy_config(mm)
    strategy_config_hash = strategy_config.config_hash()

    # (5) Maker PolicyEnvelope + policy_hash equality; manifest bound to strategy_config_hash.
    envelope = build_maker_policy_envelope(submitted, mm)
    recomputed_policy_hash = envelope.policy_hash()
    if recomputed_policy_hash != instance.policy_hash:
        raise MMAuthorityMismatchError(
            f"policy_hash mismatch: recomputed={recomputed_policy_hash!r} "
            f"instance.policy_hash={instance.policy_hash!r}"
        )
    manifest = build_maker_manifest(
        submitted, mm, operator_id=instance.operator_id or "", strategy_config_hash=strategy_config_hash
    )
    if manifest.strategy_config_hash != strategy_config_hash:
        raise MMAuthorityMismatchError("manifest.strategy_config_hash does not bind the reconstructed StrategyConfig")

    # (7) Tape — server-side injectable resolver ONLY, keyed by the persisted tape_ref.
    resolve = tape_resolver if tape_resolver is not None else default_mm_tape_resolver
    tape = resolve(mm.tape_ref)
    recomputed_tape_hash = compute_tape_content_hash(tape.events)
    if recomputed_tape_hash != tape.content_hash:
        raise MMAuthorityMismatchError(
            f"tape content_hash mismatch: recomputed={recomputed_tape_hash!r} "
            f"tape.content_hash={tape.content_hash!r}"
        )

    # (6) Bind the SAME manifest + envelope objects BY IDENTITY into FacadeDeps + MakerInstanceConfig.
    proposer_fn = proposer if proposer is not None else OfflineRecordingProposer()
    facade_deps = FacadeDeps(
        adapter=_OfflineWireSentinel("adapter"),
        signer=_OfflineWireSentinel("signer"),
        sources=_OfflineWireSentinel("sources"),
        now_fn=lambda: int(time.time()),
        sleep_fn=_no_sleep,
        envelope=envelope,
        manifest=manifest,
        wallet_equity_at_decision=_MM_WALLET_EQUITY_AT_DECISION,
        fixed_fraction=_MM_FIXED_FRACTION,
        freeze_sink=_LoggingFreezeSink(),
        proposer=proposer_fn,
        agent_id=instance.agent_id,
        run_id=ctx.run_id,
    )
    request_config = R4ARequestConfig(
        strategy_id=strategy_config.strategy_id,
        strategy_config_hash=strategy_config_hash,
        policy_hash=recomputed_policy_hash,
        session_id=ctx.session_id,
        manifest_hash=manifest.manifest_hash(),
        mode="dry_run",
        wallet_equity_at_decision=_MM_WALLET_EQUITY_AT_DECISION,
        fixed_fraction=_MM_FIXED_FRACTION,
        tif=strategy_config.tif,
    )
    resolved_session_dir = session_dir if session_dir is not None else _session_dir_for(instance.instance_id, ctx.run_id)
    instance_cfg = MakerInstanceConfig(
        strategy_config=strategy_config,
        request_config=request_config,
        manifest=manifest,
        envelope=envelope,
        facade_deps=facade_deps,
        seed_state=seed_state if seed_state is not None else StrategyState(),
        session_meta_factory=lambda t: _session_meta(strategy_config, t.identity.fixture_id),
        session_dir=resolved_session_dir,
        agent_id=instance.agent_id,
        run_id=ctx.run_id,
        session_id=ctx.session_id,
    )

    # (8) guard_enabled derived from the hash-bound StrategyConfig.
    guard_enabled = strategy_config.guard_enabled

    return instance_cfg, tape, mode, guard_enabled
