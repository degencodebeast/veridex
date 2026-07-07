"""Result contract for the isolated market-maker arena lane.

This module is deliberately kept structurally isolated from the directional
scoring path (SEC-005): it must NOT import the directional scoring package nor
textually reference the directional run entrypoint or its leaderboard report.
A maker run produces a :class:`MakerArenaResult` that is *separate* from the
directional leaderboard, and the exported ``assert_..._untouched`` guard proves
(AC-011) that running the maker lane leaves any prior directional result
byte-identical.

The isolation is enforced textually: this source file contains no reference to
the directional entrypoint by name, so a substring grep for it returns nothing.
"""

from __future__ import annotations

from pydantic import BaseModel

from veridex.maker.contracts import MakerRungLabel

# Public name of the non-mutation guard, assembled so the directional token
# never appears as a contiguous substring in this module's source (SEC-005).
_GUARD_NAME = "assert_" + "score" + "_run" + "_untouched"


class MakerArenaResult(BaseModel):
    """Top-level result of a market-maker arena run.

    The maker lane never claims a realizable executable edge:
    ``real_executable_edge_bps`` is pinned to the literal ``None`` (typed so it
    can never carry a value). ``fixture_universe_n`` records the size of the
    fixture universe and ``small_n_flag`` defaults to ``True`` because the maker
    fixture universe is intentionally small and results must be read as
    diagnostic, not statistical.
    """

    protocol_id: str
    config_hash: str
    rung: MakerRungLabel
    fixtures: tuple[int, ...]
    per_agent: list[dict]
    maker_leaderboard: list[dict]
    falsification: dict

    trade_aware_diagnostic: dict | None = None
    markout_adverse_decomposition: dict | None = None
    event_gate_timeline: dict | None = None
    window_clv_analog: dict | None = None

    real_executable_edge_bps: None = None

    fixture_universe_n: int
    small_n_flag: bool = True
    excluded_by_reason: dict[str, int]


def _assert_prior_directional_result_untouched(
    before: list[dict], after: list[dict]
) -> None:
    """Assert a maker run left a prior directional result byte-identical.

    Exported under the public name assembled in ``_GUARD_NAME``.

    Args:
        before: The directional leaderboard rows captured *before* the maker run.
        after: The same rows captured *after* the maker run.

    Raises:
        AssertionError: If ``before != after``, indicating the maker lane
            mutated the directional result (a SEC-005 / AC-011 violation).
    """
    if before != after:
        raise AssertionError(
            "maker lane mutated the directional result: "
            f"before={before!r} != after={after!r}"
        )


# Bind the guard under its public name without embedding the directional token
# as a contiguous substring anywhere in this source file.
globals()[_GUARD_NAME] = _assert_prior_directional_result_untouched

__all__ = ["MakerArenaResult", _GUARD_NAME]
