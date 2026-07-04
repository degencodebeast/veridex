"""C-1 — Polymarket feasibility probe + coverage gate (CON-001).

TDD RED-first. The pure core (:mod:`veridex.venues.polymarket_coverage`) is the hard
feasibility gate: a 1X2 fixture is *headline-eligible* iff ALL THREE sides (home/away/draw)
clear the pre-kickoff quote-count threshold; any uncovered side carries a NAMED
``partial_side_missing`` reason and demotes the fixture to diagnostic-only (never headline).
The coverage artifact is content-hashed so a predeclared run can pin the exact probe-passing
universe before any estimated-edge number exists.

Offline only — the operator probe shell's network path is NOT unit-tested here; the shell's
import-safety and fail-closed artifact-shaping are.
"""

from __future__ import annotations

from veridex.venues.polymarket_coverage import (
    SideInput,
    VenueCoverage,
    VenueSideCoverage,
    coverage_content_hash,
    evaluate_venue_coverage,
)


def _side_inputs(counts: dict[str, int]) -> dict[str, SideInput]:
    """An int-per-side count map -> the rich per-side inputs the gate consumes."""
    return {side: SideInput(pre_kickoff_quote_count=n) for side, n in counts.items()}


def _si(
    *,
    count: int,
    buckets: dict[str, int],
    first: int | None,
    last: int | None,
    token_resolved: bool = True,
) -> SideInput:
    """A rich per-side probe input (count + freshness buckets + first/last quote ts)."""
    return SideInput(
        pre_kickoff_quote_count=count,
        quote_count=count,
        freshness_bucket_counts=buckets,
        first_quote_ts=first,
        last_quote_ts=last,
        token_resolved=token_resolved,
    )


# --- the CON-001 gate: all-three-sides rule + partial -> not headline ---


def test_all_three_sides_covered_is_headline_eligible() -> None:
    counts = {"home": 8, "away": 6, "draw": 5}  # all >= 5 pre-kickoff
    cov = evaluate_venue_coverage(101, counts, kickoff_ts=1000, min_pre_kickoff=5)
    assert cov.headline_eligible is True
    assert all(s.covered for s in cov.sides)


def test_missing_draw_is_not_headline_eligible() -> None:
    counts = {"home": 8, "away": 6, "draw": 0}  # draw uncovered
    cov = evaluate_venue_coverage(102, counts, kickoff_ts=1000, min_pre_kickoff=5)
    assert cov.headline_eligible is False
    draw = next(s for s in cov.sides if s.side == "draw")
    assert draw.covered is False and draw.partial_side_missing is not None


def test_thin_side_below_threshold_excludes_fixture_from_headline() -> None:
    counts = {"home": 8, "away": 4, "draw": 5}  # away < 5
    cov = evaluate_venue_coverage(103, _side_inputs(counts), kickoff_ts=1000, min_pre_kickoff=5)
    assert cov.headline_eligible is False  # diagnostic-only, never headline
    away = next(s for s in cov.sides if s.side == "away")
    assert away.covered is False and away.partial_side_missing is not None


def test_full_coverage_artifact_shape_and_content_hash() -> None:
    # the coverage artifact IS the hard gate (Codex): freshness buckets, first/last ts, reasons, hash
    inputs = {
        "home": _si(count=8, buckets={"<=2m": 3, "<=5m": 3, "<=15m": 2}, first=100, last=900),
        "away": _si(count=0, buckets={}, first=None, last=None),  # missing -> partial reason
        "draw": _si(count=6, buckets={"<=2m": 6}, first=200, last=850),
    }
    cov = evaluate_venue_coverage(104, inputs, kickoff_ts=1000, min_pre_kickoff=5)

    home = next(s for s in cov.sides if s.side == "home")
    assert home.freshness_bucket_counts == {"<=2m": 3, "<=5m": 3, "<=15m": 2}
    assert home.first_quote_ts == 100 and home.last_quote_ts == 900
    assert isinstance(home, VenueSideCoverage)

    away = next(s for s in cov.sides if s.side == "away")
    assert away.covered is False and away.partial_side_missing  # named reason, not silent

    assert cov.headline_eligible is False  # away uncovered
    assert isinstance(cov, VenueCoverage)

    h = coverage_content_hash(cov)
    assert h and h == coverage_content_hash(cov)  # deterministic content hash of the artifact


def test_content_hash_changes_with_coverage_content() -> None:
    a = evaluate_venue_coverage(1, {"home": 8, "away": 6, "draw": 5}, kickoff_ts=1000, min_pre_kickoff=5)
    b = evaluate_venue_coverage(1, {"home": 8, "away": 6, "draw": 4}, kickoff_ts=1000, min_pre_kickoff=5)
    assert coverage_content_hash(a) != coverage_content_hash(b)  # a real content hash, not a constant


# --- operator probe shell: import-safety (network-free) + fail-closed artifact shaping ---


def test_probe_shell_imports_network_free() -> None:
    # CON-010: all veridex/network imports are LAZY inside functions, so importing the shell
    # module does no network and needs no credentials.
    import scripts.txline_live.cp1_probe as probe

    assert callable(probe.build_coverage_artifact)
    assert callable(probe.render_summary)


def test_probe_refuses_to_write_an_empty_artifact() -> None:
    import pytest

    from scripts.txline_live.cp1_probe import build_coverage_artifact

    with pytest.raises(ValueError):
        build_coverage_artifact([])  # nothing probed is a misconfiguration, not a result


def test_zero_headline_eligible_still_produces_a_fail_closed_artifact() -> None:
    # CON-001 fail-closed: zero headline-eligible fixtures is a legitimate (dead) result — the
    # coverage artifact IS the deliverable and the summary says NOT VIABLE, loudly.
    from scripts.txline_live.cp1_probe import build_coverage_artifact, render_summary

    cov = evaluate_venue_coverage(
        200, {"home": 8, "away": 6, "draw": 0}, kickoff_ts=1000, min_pre_kickoff=5
    )
    artifact = build_coverage_artifact([cov])

    assert artifact["headline_eligible_count"] == 0
    assert artifact["viable"] is False
    assert artifact["artifact_content_hash"]
    assert any("NOT VIABLE" in line for line in render_summary(artifact))


def test_artifact_records_the_threshold_actually_used() -> None:
    # the recorded min_pre_kickoff must be the SAME threshold that produced the covered flags,
    # not a hardcoded constant that could silently disagree.
    from scripts.txline_live.cp1_probe import build_coverage_artifact

    cov = evaluate_venue_coverage(
        300, {"home": 8, "away": 6, "draw": 5}, kickoff_ts=1000, min_pre_kickoff=7
    )
    assert cov.headline_eligible is False  # away(6) and draw(5) are below 7
    artifact = build_coverage_artifact([cov], min_pre_kickoff=7)
    assert artifact["min_pre_kickoff"] == 7


def test_headline_eligible_artifact_is_viable() -> None:
    from scripts.txline_live.cp1_probe import build_coverage_artifact, render_summary

    cov = evaluate_venue_coverage(
        201, {"home": 8, "away": 6, "draw": 5}, kickoff_ts=1000, min_pre_kickoff=5
    )
    artifact = build_coverage_artifact([cov])

    assert artifact["headline_eligible_count"] == 1
    assert artifact["headline_eligible_fixture_ids"] == [201]
    assert artifact["viable"] is True
    assert not any("NOT VIABLE" in line for line in render_summary(artifact))
