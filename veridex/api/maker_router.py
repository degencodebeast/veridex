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

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from veridex.api.schemas import MakerArenaResultResponse
from veridex.maker.result import MakerArenaResult, render_proof_card

if TYPE_CHECKING:
    from collections.abc import Callable

    from veridex.mm_strategy.composition import GuardAblationResult, SessionSummary

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


def register_maker_routes(
    app: FastAPI,
    *,
    live_ab_provider: Callable[[str], GuardAblationResult | None] | None = None,
) -> None:
    """Register the read-only maker routes onto ``app`` (SEC-005 lane).

    Mounts ``GET /maker/arena-result`` (the sealed maker leaderboard envelope) and the II-3
    ``GET /maker/live-ab/{instance_id}`` guard ON/OFF behavior-ablation projection. Both are read-only.

    Args:
        app: The FastAPI application to mount the maker routes on.
        live_ab_provider: Optional lookup returning the guard ON/OFF ``GuardAblationResult`` for an
            instance id (``None`` when none is available → the ablation route 404s). Defaults to ``None``
            so the real app mounts the route as a read-only projection with no ablation wired yet.
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

    @app.get("/maker/live-ab/{instance_id}", response_model=LiveGuardAblationResponse)
    async def get_live_ab(instance_id: str) -> LiveGuardAblationResponse:
        """Return the guard ON/OFF behavior ablation for ``instance_id`` (read-only; NOT a rank).

        Projects the II-3 ``run_guard_ablation`` result — the SAME strategy on the SAME pinned tape with
        the QuoteGuard OFF vs ON — as a matched ablation panel: the two arms' decisions + the pinned-frame
        divergence, under explicit ablation-not-ranking labels. It emits NO rank / toxicity / performance
        ordering / winner and never touches the sealed historical maker leaderboard.

        Returns:
            A :class:`LiveGuardAblationResponse`.

        Raises:
            HTTPException: 404 when no ablation is available for the instance (never a fabricated result).
        """
        result = live_ab_provider(instance_id) if live_ab_provider is not None else None
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"no guard on/off ablation available for instance {instance_id!r}",
            )
        return build_live_ab_projection(result, instance_id=instance_id)
