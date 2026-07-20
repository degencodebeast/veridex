"""Read-only maker arena API lane — serves the SEALED ``MakerArenaResult`` over HTTP.

This module is the Option-2 maker-UI bridge: it exposes the sealed
``scripts/txline_live/cp1/maker-arena-result.json`` artifact so the frontend can render the
maker leaderboard WITHOUT the backend re-running the maker arena.

Structural isolation (SEC-005): this module is a SEPARATE namespace from the directional
scoring path. It deliberately imports NOTHING from the directional scorer/leaderboard and
textually references no directional entrypoint (a source scan of this module for the
directional scorer function or its package names returns nothing — see the SEC-005 test).
The maker rank axis is ``avg_toxicity_loss_bps`` (ascending — lower is better); it is never
relabelled as a CLV/PnL/edge/return metric, and every ``real_executable_edge_bps`` stays the
literal ``None`` (no fill or PnL claim).

The route is registered onto the shared app via :func:`register_maker_routes`, mirroring the
``register_deploy_routes`` / ``register_arena_routes`` composition pattern already used by the
FastAPI factory. The envelope builder :func:`build_maker_arena_result_response` is the single
source of truth reused by both the route and the frozen-fixture generator
(``scripts/gen_maker_fixture.py``) — the fixture is never hand-authored.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from veridex.api.auth_privy import PrivyPrincipal
from veridex.api.schemas import MakerArenaResultResponse
from veridex.deploy.preflight import MM_STRATEGY_FAMILY
from veridex.maker.result import MakerArenaResult, render_proof_card

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from veridex.deploy.instance import AgentInstance
    from veridex.mm_strategy.composition import GuardAblationResult, SessionSummary
    from veridex.store import Store

# maker_router -> api -> veridex -> repo root; the sealed maker artifact hangs off it.
# Mirrors ``veridex.maker.runner.RESULT_PATH`` without importing the runner (which would drag in
# the maker scorer/tape machinery this read-only lane does not need).
_REPO_ROOT = Path(__file__).resolve().parents[2]
SEALED_RESULT_PATH: Path = _REPO_ROOT / "scripts" / "txline_live" / "cp1" / "maker-arena-result.json"

#: Axis-honesty labels: the ONE rank axis vs the diagnostics that must never be read as a rank.
#: ``real_executable_edge_bps`` is pinned null everywhere — the maker lane claims no fill/PnL.
MAKER_DIAGNOSTICS: dict[str, str] = {
    "avg_markout_bps_label": "diagnostic_not_rank_axis",
    "avg_toxicity_loss_bps_label": "rank_axis_lower_is_better",
    "real_executable_edge_bps_label": "always_null_no_fill_or_pnl_claim",
    # The MM-R1.5 trade-aware diagnostic is report-only: it carries adverse-selection /
    # convergence observations, NEVER a fill / fill-rate / spread-capture-as-PnL /
    # realized-PnL / executable-edge value. Null when the diagnostic was not run.
    "trade_aware_diagnostic_label": "report_only_no_fill_or_edge_claim",
}


def build_maker_arena_result_response(
    result_path: Path | None = None,
) -> MakerArenaResultResponse:
    """Build the maker envelope from the sealed artifact (single source of truth).

    Loads the sealed JSON, parses it into a :class:`~veridex.maker.result.MakerArenaResult`
    (round-tripping through the model preserves every sealed field: ``rung``,
    ``fixture_universe_n``, ``small_n_flag``, each ``maker_leaderboard`` row's
    ``maker_rank`` / ``avg_toxicity_loss_bps`` / ``avg_markout_bps`` /
    ``real_executable_edge_bps``, the top-level ``real_executable_edge_bps``,
    ``falsification.verdict`` and ``window_clv_analog.note``), renders the honest proof card,
    and assembles the frozen envelope.

    Args:
        result_path: Override for the sealed artifact path (defaults to
            :data:`SEALED_RESULT_PATH`). Used by the fixture generator + tests.

    Returns:
        A :class:`~veridex.api.schemas.MakerArenaResultResponse`.

    Raises:
        FileNotFoundError: When the sealed artifact is absent (the route maps this to 404).
    """
    path = result_path if result_path is not None else SEALED_RESULT_PATH
    if not path.is_file():
        raise FileNotFoundError(f"sealed maker arena result not found: {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    result = MakerArenaResult.model_validate(raw)
    proof_card = render_proof_card(result)

    return MakerArenaResultResponse(
        result=result.model_dump(mode="json"),
        proof_card=proof_card.model_dump(mode="json"),
        diagnostics=dict(MAKER_DIAGNOSTICS),
    )


# ---------------------------------------------------------------------------------------------
# II-3 — the guard ON/OFF biting-beat ABLATION projection (read-only; NOT a maker leaderboard).
# ---------------------------------------------------------------------------------------------
# This lane surfaces the II-3 ``run_guard_ablation`` result: the SAME maker strategy on the SAME pinned
# tape with the TxLINE QuoteGuard OFF vs ON. It is a BEHAVIOR ablation (does the guard change the
# decision?), NEVER a rank / toxicity / performance ordering / winner — that would be a false-edge
# honesty violation, conflating it with the SEPARATE sealed HISTORICAL maker leaderboard served above.
# The projection therefore carries only the two arms' decisions + the pinned-frame divergence, under
# explicit ablation-not-ranking labels, and imports NOTHING from the scorer / rank machinery.

#: Honesty labels the guard ON/OFF projection wears: it is a BEHAVIOR ablation, not a ranked result. The
#: negations live in the label VALUES (readable disclaimers); the KEYS carry no rank/ordering token.
LIVE_AB_ABLATION_LABELS: dict[str, str] = {
    "panel_kind": "behavior_ablation_guard_off_vs_on",
    "comparison_basis": "same strategy, same pinned tape — only the QuoteGuard arm differs",
    "panel_disclaimer": (
        "this demonstrates the guard CHANGES behavior; it is NOT a rank / toxicity / performance "
        "ordering / winner and is never conflated with the sealed historical maker leaderboard"
    ),
    "divergence_scope": (
        "divergence reports only the frames where the two matched arms actually differed on this tape; "
        "it may be empty and is never promised — an observed behavior difference, not a guarantee the "
        "guard always diverges"
    ),
}

#: The ablation-code / schema version folded into the memo key. Bump this whenever the reconstruct or
#: ``run_guard_ablation`` behavior changes so a stale cached result can never be served under new code.
LIVE_AB_ABLATION_VERSION = "maker_live_ab.ablation.v1"


class LiveGuardAblationResponse(BaseModel):
    """Read-only envelope for ``GET /maker/live-ab/{instance_id}`` — the guard ON/OFF behavior ablation.

    A projection of the II-3 ``GuardAblationResult``: the SAME strategy on the SAME pinned tape with the
    guard OFF then ON. It carries ONLY the two arms' decision streams + the pinned-frame divergence under
    explicit ablation-not-ranking labels. There is deliberately NO rank / toxicity / ordering / winner
    field anywhere in this envelope — the ablation shows a BEHAVIOR change, never a ranked maker result.

    Attributes:
        schema_version: Frozen envelope version tag (``"maker_live_ab.v1"``).
        lane: Always ``"maker"``.
        panel: Always ``"guard_on_off_ablation"`` — the matched ablation panel, not a leaderboard.
        is_ablation: Always ``True`` — this is a behavior ablation (guard off vs on), not a ranking.
        instance_id: The maker instance the ablation was run for.
        mode: The replay/dry-run mode both arms ran under.
        guard_off / guard_on: Each arm's honest projection (decisions + terminal reason + counts).
        divergent_frame_indices: The frames on which the two arms' substantive decision diverged.
        diverges: Whether the guard flip changed the decision on at least one frame of this tape.
        labels: The honesty labels marking this an ablation, never a rank.
    """

    schema_version: str = "maker_live_ab.v1"
    lane: str = "maker"
    panel: str = "guard_on_off_ablation"
    is_ablation: bool = True
    instance_id: str
    mode: str
    guard_off: dict[str, Any]
    guard_on: dict[str, Any]
    divergent_frame_indices: list[int]
    diverges: bool
    labels: dict[str, str]


def _project_arm(summary: SessionSummary) -> dict[str, Any]:
    """Project ONE ablation arm's :class:`SessionSummary` into an honest, rank-free JSON shape.

    Surfaces the arm's BEHAVIOR only — its guard flag, per-frame decisions (kind + closed reason codes +
    priced legs), terminal reason, and frame count. No score / rank / toxicity / edge / PnL field.
    """
    decisions: list[dict[str, Any]] = []
    for index, decision in enumerate(summary.decisions):
        legs = [
            {
                "kind": leg.kind,
                "role": leg.leg_role,
                "price": leg.price,
                "post_only": leg.post_only,
            }
            for leg in decision.intent_plan
        ]
        decisions.append(
            {
                "index": index,
                "kind": decision.kind,
                "reason_codes": list(decision.reason_codes),
                "legs": legs,
            }
        )
    return {
        "guard_enabled": summary.guard_enabled,
        "terminal_reason": summary.terminal_reason,
        "observations_consumed": summary.observations_consumed,
        "decisions": decisions,
    }


def build_live_ab_projection(
    result: GuardAblationResult, *, instance_id: str
) -> LiveGuardAblationResponse:
    """Build the guard ON/OFF ablation envelope from a ``GuardAblationResult`` (single source of truth).

    Reused by both the ``GET /maker/live-ab/{instance_id}`` route and any fixture/tests. Projects the two
    arms' behavior + the pinned-frame divergence under the ablation-not-ranking labels — it never emits,
    computes, or relabels a rank / toxicity / performance ordering / winner.

    Args:
        result: The II-3 paired guard-off/guard-on ablation result.
        instance_id: The maker instance the ablation was run for.

    Returns:
        A :class:`LiveGuardAblationResponse`.
    """
    return LiveGuardAblationResponse(
        instance_id=instance_id,
        mode=result.mode,
        guard_off=_project_arm(result.guard_off),
        guard_on=_project_arm(result.guard_on),
        divergent_frame_indices=list(result.divergent_frame_indices),
        diverges=result.diverges,
        labels=dict(LIVE_AB_ABLATION_LABELS),
    )


async def _reconstruct_and_run_ablation(instance: AgentInstance) -> GuardAblationResult:
    """The PRODUCTION owner-scoped A/B compute: reconstruct ONLY from server-owned state, run OFF/ON.

    Builds the SERVER-owned :class:`RunContext` from the persisted instance, reconstructs the
    ``run_market_maker`` session bundle via :func:`reconstruct_mm_session` (``tape_resolver=None`` → the
    production catalog; an :class:`OfflineRecordingProposer` for the reconstruct proposer arg — offline,
    no wire), and runs the guard OFF/ON ablation. The client supplies NOTHING here — no run_id / tape /
    config / hash / decisions.

    NON-EXECUTING by construction: the ablation is FORCED to ``mode="replay"`` (never the reconstructed
    ``replay_dry_run``), so ``run_market_maker`` takes the pure decision-replay path — it folds the tape
    and decides, but NEVER drives ``execute_plan_bridged`` / the R4-A dry-run proposer / the wallet /
    venue. The guard decisions and their divergence are identical to the executing path; only the
    discarded dry-run receipts are skipped. The OPS ``event_sink`` is a LOCAL no-op, so the read-only
    A/B emits nothing to the durable OPS channel, produces no receipt, and never touches the sealed
    maker arena result / any leaderboard.

    Fail-closed reconstruct errors (config/policy/tape-hash drift, non-replay mode, unknown tape)
    propagate as ``ValueError`` / ``LookupError`` for the route to map to a 409.

    Args:
        instance: The persisted, server-owned :class:`AgentInstance` (already ownership-checked).

    Returns:
        The paired guard OFF/ON :class:`GuardAblationResult` (a behavior ablation, never a rank).
    """
    # Local imports keep this read-only lane's TOP-LEVEL import surface minimal (SEC-005) and defer the
    # heavier MM-strategy graph until the owner-scoped A/B is actually computed.
    from veridex.mm_strategy import session_factory as sf
    from veridex.mm_strategy.composition import run_guard_ablation
    from veridex.mm_strategy.offline_proposer import OfflineRecordingProposer
    from veridex.runtime.mm_agent_adapter import RunContext

    ctx = RunContext(
        run_id=instance.run_id,
        session_id="live-ab",
        runtime_agent_id="live-ab",
        owner_did=instance.operator_id or "",
    )
    with tempfile.TemporaryDirectory(prefix="veridex-live-ab-") as tmp:
        cfg, _tape, _mode, _guard = sf.reconstruct_mm_session(
            instance,
            ctx,
            tape_resolver=None,
            proposer=OfflineRecordingProposer(),
            session_dir=Path(tmp),
        )
        # FORCE mode="replay" (ignore the reconstructed mode): a behavior-only, NON-executing read that
        # never drives the R4-A dry-run bridge. Isolated no-op sink — NOT the real OPS sink.
        return await run_guard_ablation(cfg, _tape, mode="replay", event_sink=lambda _e: None)


def _authority_fingerprint(instance: AgentInstance) -> str:
    """Run the reconstruction AUTHORITY GATE and return a fingerprint of the verified frozen inputs.

    This calls :func:`reconstruct_mm_session` — the gate that recomputes and re-verifies ``config_hash``,
    ``policy_hash``, and the resolved tape's ``content_hash`` — on EVERY request, BEFORE the expensive
    ``run_guard_ablation`` result cache is consulted. Any drift raises ``ValueError`` / ``LookupError``
    (mapped to 409 by the route), so a warm cache can never launder a later authority mutation; reconstruct
    is cheap relative to the ablation (which runs the maker twice over ~300 decisions), so only the
    ablation result is cached.

    The fingerprint covers every persisted input whose drift reconstruction rejects — ``instance_id``,
    ``config_hash``, ``policy_hash``, the mode pair (``source_mode`` / ``execution_mode``), the freshly
    resolved tape ``content_hash`` + ``fixture_id``, and the ablation schema version — so a cached result
    is only ever reused for a byte-identical, re-verified authority. The cache is an optimization ONLY,
    never a scoring/proof authority.

    Raises:
        ValueError / LookupError: Any reconstruction authority mismatch (drift / non-replay / unknown tape).
    """
    from veridex.mm_strategy import session_factory as sf
    from veridex.mm_strategy.offline_proposer import OfflineRecordingProposer
    from veridex.runtime.mm_agent_adapter import RunContext

    ctx = RunContext(
        run_id=instance.run_id,
        session_id="live-ab",
        runtime_agent_id="live-ab",
        owner_did=instance.operator_id or "",
    )
    # reconstruct writes nothing to session_dir (only run_market_maker's recorder does), but a throwaway
    # tempdir keeps the derived per-run session path from being created as a side effect of this gate.
    with tempfile.TemporaryDirectory(prefix="veridex-live-ab-auth-") as tmp:
        _cfg, tape, _mode, _guard = sf.reconstruct_mm_session(
            instance,
            ctx,
            tape_resolver=None,
            proposer=OfflineRecordingProposer(),
            session_dir=Path(tmp),
        )
    parts = [
        instance.instance_id,
        instance.config_hash,
        instance.policy_hash,
        instance.source_mode,
        instance.execution_mode,
        tape.content_hash,
        str(int(tape.identity.fixture_id)),
        LIVE_AB_ABLATION_VERSION,
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def register_maker_routes(
    app: FastAPI,
    *,
    store: Store | None = None,
    require_principal: Callable[..., PrivyPrincipal] | None = None,
    ab_compute: Callable[[AgentInstance], Awaitable[GuardAblationResult]] | None = None,
    live_ab_provider: Callable[[str], GuardAblationResult | None] | None = None,
) -> None:
    """Register the read-only maker routes onto ``app`` (SEC-005 lane).

    Mounts ``GET /maker/arena-result`` (the sealed maker leaderboard envelope) and the II-3
    ``GET /maker/live-ab/{instance_id}`` guard ON/OFF behavior-ablation projection. Both are read-only.

    The ``live-ab`` route has two registration modes:

    * **Owner-scoped (production)** — when BOTH ``store`` and ``require_principal`` are supplied. The
      route authenticates the caller (401 anonymous), derives ownership SERVER-SIDE from the persisted
      instance (404 unknown/unowned, 403 cross-owner, 404 non-maker), reconstructs the A/B ONLY from
      server-owned state via ``ab_compute`` (defaulting to :func:`_reconstruct_and_run_ablation`), and
      memoizes the result under the frozen-input key with a per-key single-flight lock. Fail-closed
      reconstruct/mode errors map to 409.
    * **Legacy provider (tests)** — when ``require_principal``/``store`` are absent. Preserves the prior
      no-auth ``live_ab_provider`` seam (404 when it returns ``None``) so existing callers keep working.

    Args:
        app: The FastAPI application to mount the maker routes on.
        store: The :class:`~veridex.store.Store` the owner-scoped route loads instances from.
        require_principal: The Privy ``require_principal`` dependency (owner-scoped auth boundary).
        ab_compute: Injectable A/B compute seam (``AgentInstance -> GuardAblationResult``); defaults to
            the production reconstruct+ablation service. Wrapped by the memo + single-flight guard.
        live_ab_provider: Legacy no-auth lookup returning a ``GuardAblationResult`` for an instance id
            (``None`` → 404). Used only when the owner-scoped path is not wired.
    """

    @app.get("/maker/arena-result", response_model=MakerArenaResultResponse)
    async def get_maker_arena_result() -> MakerArenaResultResponse:
        """Return the sealed maker arena result as a frozen, read-only envelope.

        Reads the sealed ``maker-arena-result.json`` artifact ONLY — it never re-runs the maker
        arena and never touches the directional scoring path (SEC-005). The maker leaderboard is
        served in its native shape (ranked ascending by ``avg_toxicity_loss_bps``), never reshaped
        into the directional leaderboard and never relabelled as CLV/PnL/edge/return.

        Returns:
            A :class:`~veridex.api.schemas.MakerArenaResultResponse`.

        Raises:
            HTTPException: 404 when the sealed artifact is absent (rather than crashing).
        """
        try:
            return build_maker_arena_result_response()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    if store is not None and require_principal is not None:
        # --- OWNER-SCOPED production path -----------------------------------------------------
        _dep_store: Store = store
        _dep_principal = require_principal
        _compute: Callable[[AgentInstance], Awaitable[GuardAblationResult]] = (
            ab_compute if ab_compute is not None else _reconstruct_and_run_ablation
        )
        # Per-app memo + single-flight state (fresh per registration so tests never share cache state).
        # Keyed by the AUTHORITY FINGERPRINT (a hash over the re-verified frozen inputs), never a raw
        # instance id — so a cached result is only ever reused for a byte-identical, re-verified authority.
        _result_cache: dict[str, GuardAblationResult] = {}
        _locks: dict[str, asyncio.Lock] = {}

        def _lock_for(key: str) -> asyncio.Lock:
            # setdefault-style get-or-create is atomic on the asyncio event loop (no await between the
            # miss and the insert), so concurrent duplicate reads share ONE lock per frozen-input key.
            lock = _locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                _locks[key] = lock
            return lock

        async def _memoized_ab(instance: AgentInstance) -> GuardAblationResult:
            """Reconstruct-verify on EVERY request, then memoize ONLY the expensive ablation.

            :func:`_authority_fingerprint` re-runs the reconstruction authority gate (config/policy/tape
            hash re-verify) BEFORE the cache is consulted, so a warm cache can never launder a later
            authority mutation — drift raises here and the route maps it to 409. Only the paired
            ``run_guard_ablation`` result (the maker run twice) is cached, under a per-key single-flight
            lock so concurrent duplicate reads compute it once.
            """
            key = _authority_fingerprint(instance)
            async with _lock_for(key):
                cached = _result_cache.get(key)
                if cached is not None:
                    return cached
                result = await _compute(instance)
                _result_cache[key] = result
                return result

        @app.get("/maker/live-ab/{instance_id}", response_model=LiveGuardAblationResponse)
        async def get_live_ab(  # noqa: B008
            instance_id: str,
            principal: PrivyPrincipal = Depends(_dep_principal),  # noqa: B008
        ) -> LiveGuardAblationResponse:
            """Return the OWNER'S guard ON/OFF behavior ablation for ``instance_id`` (read-only; NOT a rank).

            Trust boundary: ownership is derived SERVER-SIDE from the persisted instance (the client
            supplies NO run_id / tape / config / hash / decisions). Fail-closed exactly like the sibling
            owner-scoped reads (I-2/I-4): 404 for absent OR unowned/legacy (no existence leak), 403 for
            owned-by-another, 404 for a directional (non-maker) instance (it has no guard ablation — and
            is refused BEFORE any reconstruction). The A/B is reconstructed ONLY from server-owned state
            and run OFFLINE; a drift / non-replay / reconstruct failure maps to 409 (fail closed).

            Returns:
                A :class:`LiveGuardAblationResponse` (the two arms' decisions + pinned-frame divergence).

            Raises:
                HTTPException: 401 (unauthenticated), 404 (absent/unowned/non-maker — no leak), 403
                    (wrong owner), 409 (reconstruct/mode fail-closed).
            """
            try:
                instance = await _dep_store.get_agent_instance(instance_id)
            except KeyError:
                raise HTTPException(status_code=404, detail="agent instance not found") from None
            # Fail-closed: an unowned / legacy row is never inherited — hide it as if it does not exist.
            if instance.operator_id is None:
                raise HTTPException(status_code=404, detail="agent instance not found")
            if instance.operator_id != principal.did:
                raise HTTPException(
                    status_code=403, detail="principal does not own this agent instance"
                )
            # A directional instance has no guard ablation. Refuse BEFORE any reconstruction, and hide it
            # as a 404 (never a 409) so the strategy gate can't be probed as an oracle for existence.
            if instance.submitted_config.get("strategy") != MM_STRATEGY_FAMILY:
                raise HTTPException(status_code=404, detail="agent instance not found")
            try:
                result = await _memoized_ab(instance)
            except (ValueError, LookupError) as exc:
                # Drift / non-replay mode / unknown tape / any reconstruct authority mismatch: fail closed.
                raise HTTPException(
                    status_code=409,
                    detail="guard ablation unavailable for this instance",
                ) from exc
            return build_live_ab_projection(result, instance_id=instance_id)

    else:
        # --- LEGACY no-auth provider path (preserved for existing callers/tests) --------------
        @app.get("/maker/live-ab/{instance_id}", response_model=LiveGuardAblationResponse)
        async def get_live_ab_legacy(instance_id: str) -> LiveGuardAblationResponse:
            """Return the guard ON/OFF behavior ablation for ``instance_id`` (read-only; NOT a rank).

            Projects the II-3 ``run_guard_ablation`` result — the SAME strategy on the SAME pinned tape
            with the QuoteGuard OFF vs ON — as a matched ablation panel: the two arms' decisions + the
            pinned-frame divergence, under explicit ablation-not-ranking labels. It emits NO rank /
            toxicity / performance ordering / winner and never touches the sealed maker leaderboard.

            Returns:
                A :class:`LiveGuardAblationResponse`.

            Raises:
                HTTPException: 404 when no ablation is available for the instance (never fabricated).
            """
            result = live_ab_provider(instance_id) if live_ab_provider is not None else None
            if result is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"no guard on/off ablation available for instance {instance_id!r}",
                )
            return build_live_ab_projection(result, instance_id=instance_id)
