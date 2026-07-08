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

from fastapi import FastAPI, HTTPException

from veridex.api.schemas import MakerArenaResultResponse
from veridex.maker.result import MakerArenaResult, render_proof_card

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


def register_maker_routes(app: FastAPI) -> None:
    """Register the read-only ``GET /maker/arena-result`` route onto ``app`` (SEC-005 lane).

    Args:
        app: The FastAPI application to mount the maker route on.
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
