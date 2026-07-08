"""Backend tests for the read-only maker arena API lane (Option 2 maker-UI bridge).

Test strategy: FastAPI TestClient over the real ``create_app`` factory (offline; the route
reads the SEALED ``scripts/txline_live/cp1/maker-arena-result.json`` artifact — it never
re-runs the maker arena). The maker lane is structurally isolated from the directional
scoring path (SEC-005): the handler module must never reference ``score_run`` /
``veridex.scoring`` / ``veridex.leaderboard``.

RED-watch targets (each observed to fail before the route existed):
  - test_get_maker_arena_result_200_envelope: 404 (no route registered)
  - test_maker_router_module_has_no_directional_reference: ModuleNotFoundError (no module)
"""

from __future__ import annotations

import inspect

from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.store import InMemoryStore


def _client() -> TestClient:
    """Build a TestClient over the real app factory (backed by a fresh InMemoryStore)."""
    return TestClient(create_app(store=InMemoryStore()))


# ---------------------------------------------------------------------------
# GET /maker/arena-result — the sealed maker envelope
# ---------------------------------------------------------------------------


def test_get_maker_arena_result_200_envelope() -> None:
    """GET /maker/arena-result returns 200 with the frozen maker envelope shape."""
    resp = _client().get("/maker/arena-result")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Envelope framing (NOT the directional leaderboard; asc rank on toxicity).
    assert body["schema_version"] == "maker_arena_result.v1"
    assert body["lane"] == "maker"
    assert body["source_mode"] == "replay"
    assert body["rank_axis"] == "avg_toxicity_loss_bps"
    assert body["rank_axis_direction"] == "asc"


def test_maker_result_preserves_sealed_scalars() -> None:
    """The sealed result scalars pass through UNCHANGED (rung / n / small_n / null edge)."""
    body = _client().get("/maker/arena-result").json()
    result = body["result"]

    assert result["rung"] == "MM-R1"
    assert result["fixture_universe_n"] == 18
    assert result["small_n_flag"] is True
    # Never a fill or PnL claim — top-level executable edge is pinned null.
    assert result["real_executable_edge_bps"] is None


def test_maker_leaderboard_ranked_asc_by_toxicity_with_null_edge() -> None:
    """maker_leaderboard is rank-1-first, ascending on toxicity, every row's edge null."""
    body = _client().get("/maker/arena-result").json()
    board = body["result"]["maker_leaderboard"]

    assert board[0]["maker_rank"] == 1
    assert board[0]["avg_toxicity_loss_bps"] <= board[1]["avg_toxicity_loss_bps"]
    for row in board:
        assert row["real_executable_edge_bps"] is None


def test_maker_leaderboard_carries_real_sealed_numbers() -> None:
    """Rank 1 is txline-fair-mm @ 129 bps toxicity, ahead of naive-mm @ 172 bps."""
    body = _client().get("/maker/arena-result").json()
    board = body["result"]["maker_leaderboard"]

    assert board[0]["agent_id"] == "txline-fair-mm"
    assert board[0]["avg_toxicity_loss_bps"] == 129
    assert board[1]["agent_id"] == "naive-mm"
    assert board[1]["avg_toxicity_loss_bps"] == 172
    assert board[0]["avg_toxicity_loss_bps"] < board[1]["avg_toxicity_loss_bps"]


def test_maker_leaderboard_rows_have_no_directional_pnl_keys() -> None:
    """No CLV/PnL/edge-return key ever leaks into a maker leaderboard row (no relabelling)."""
    body = _client().get("/maker/arena-result").json()
    board = body["result"]["maker_leaderboard"]

    banned = {"avg_clv_bps", "total_clv_bps", "sim_pnl", "pnl"}
    for row in board:
        assert banned.isdisjoint(row.keys()), f"directional key leaked into maker row: {row.keys()}"


def test_maker_proof_card_and_diagnostics_present() -> None:
    """proof_card carries rung/n_fixtures/falsification; diagnostics label the axes honestly."""
    body = _client().get("/maker/arena-result").json()

    card = body["proof_card"]
    assert card["rung"] == "MM-R1"
    assert card["n_fixtures"] == 18
    assert card["falsification"]["verdict"] == "SEPARATED"

    diag = body["diagnostics"]
    assert diag["avg_markout_bps_label"] == "diagnostic_not_rank_axis"
    assert diag["avg_toxicity_loss_bps_label"] == "rank_axis_lower_is_better"
    assert diag["real_executable_edge_bps_label"] == "always_null_no_fill_or_pnl_claim"


def test_falsification_and_window_note_preserved() -> None:
    """falsification.verdict + window_clv_analog.note survive unchanged from the seal."""
    body = _client().get("/maker/arena-result").json()
    result = body["result"]

    assert result["falsification"]["verdict"] == "SEPARATED"
    assert "NOT a CLV rank axis" in result["window_clv_analog"]["note"]


# ---------------------------------------------------------------------------
# SEC-005 source-scan — the maker handler must not touch the directional path
# ---------------------------------------------------------------------------


def test_maker_router_module_has_no_directional_reference() -> None:
    """The maker route module textually references no directional scorer/leaderboard (SEC-005)."""
    import veridex.api.maker_router as maker_router

    src = inspect.getsource(maker_router)
    assert "score_run" not in src
    assert "veridex.scoring" not in src
    assert "veridex.leaderboard" not in src
