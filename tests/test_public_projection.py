"""B1 — sealed→public projection adapter tests.

Proves the adapter maps each sealed per-run score row (keyed by the runtime agent id)
to a PUBLIC row whose leaderboard-input ``agent_id`` HOLDS the ``public_agent_id`` — so
the unchanged pure aggregator :func:`veridex.leaderboard.leaderboard` groups by the public
id — while carrying replay provenance and NEVER mutating the sealed input.
"""

from __future__ import annotations

import pytest

from veridex.public_projection import (
    ProjectionError,
    PublicBinding,
    project_public_rows,
)

SEALED = [
    {
        "agent_id": "official-baseline-v1",
        "total_clv_bps": -2474,
        "action_count": 196,
        "avg_clv_bps": -12.6,
        "sim_pnl": 0,
        "brier": None,
        "max_drawdown": 0.0,
        "valid_pct": 100.0,
        "proof_mode": "reproducible",
    }
]


def test_sets_agent_id_to_public_and_preserves_sealed() -> None:
    bindings = {
        "official-baseline-v1": PublicBinding(
            public_agent_id="agt_base", instance_id="inst_a", config_hash="h"
        )
    }
    out = project_public_rows(SEALED, bindings, run_id="r1", source_mode="replay")

    assert out[0]["agent_id"] == "agt_base"
    assert out[0]["public_agent_id"] == "agt_base"
    assert out[0]["runtime_agent_id"] == "official-baseline-v1"
    assert out[0]["run_id"] == "r1"
    assert out[0]["source_mode"] == "replay"
    assert out[0]["instance_id"] == "inst_a"
    assert out[0]["config_hash"] == "h"
    assert out[0]["total_clv_bps"] == -2474  # payload carried

    # Input NOT mutated.
    assert SEALED[0]["agent_id"] == "official-baseline-v1"


def test_fails_closed_on_unbound_runtime_id() -> None:
    with pytest.raises(ProjectionError):
        project_public_rows(SEALED, {}, run_id="r1", source_mode="replay")


def test_feeds_unchanged_leaderboard() -> None:
    from veridex.leaderboard import leaderboard

    bindings = {
        "official-baseline-v1": PublicBinding(
            public_agent_id="agt_base", instance_id="inst_a", config_hash="h"
        )
    }
    out = project_public_rows(SEALED, bindings, run_id="r1", source_mode="replay")
    rows = leaderboard(out)
    assert rows[0]["agent_id"] == "agt_base"
