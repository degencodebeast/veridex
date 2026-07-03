"""T15 — BacktestReport: honest, derived-only report over a sealed RunResult (REQ-2D-303/304).

The report is a PURE FUNCTION of ``RunResult`` + ``score_rows`` — no venue/live/LLM input — so it
can never smuggle in a fresh trust claim (SEC-003). The mode ladder is a TOTAL function whose
labels NEVER lie: a backtest (replay × paper) resolves to "Backtest", never "Live"/"Live Guarded".
"""

from __future__ import annotations

import pytest

from tests._arena_fixtures import finished_run_result
from veridex.backtest.report import (
    BacktestReport,
    build_backtest_report,
    mode_ladder_label,
)
from veridex.runtime.orchestrator import RunResult
from veridex.runtime.window import RunWindow
from veridex.scoring import score_run


def _window() -> RunWindow:
    return RunWindow(
        window_id="w_report",
        fixture_id=17588404,
        market_allowlist=["OVERUNDER", "1X2"],
        end_rule="pre_match",
        min_clv_horizon_s=0,
    )


def test_report_is_pure_function_of_runresult_only() -> None:
    """A report derives ENTIRELY from a RunResult + config — no venue/live/LLM input needed."""
    run = finished_run_result(source_mode="replay")

    report = build_backtest_report(
        run,
        window=_window(),
        pack_id="pack_synthetic",
        content_hash="deadbeefcafe",
        source_mode="replay",
        execution_mode="paper",
        policy_envelope=None,
    )

    assert isinstance(report, BacktestReport)
    # Lineage is bound verbatim from the (config) inputs — the report does not re-read a venue.
    assert report.pack_id == "pack_synthetic"
    assert report.content_hash == "deadbeefcafe"
    assert report.window_id == "w_report"
    assert report.market_universe == ["OVERUNDER", "1X2"]


def test_report_has_every_spec_4_4_field() -> None:
    """Every §4.4 field is present (or explicit null-with-warning) — none silently omitted."""
    run = finished_run_result(source_mode="replay")
    report = build_backtest_report(
        run,
        window=_window(),
        pack_id="p",
        content_hash="h",
        source_mode="replay",
        execution_mode="paper",
        policy_envelope=None,
    )

    for field in (
        "window_id",
        "config_hash",
        "market_universe",
        "sample_size",
        "valid_count",
        "clv_confidence",
        "avg_clv",
        "clv_distribution",
        "sim_pnl",
        "threshold_sensitivity",
        "stale_rejected_quote_rate",
        "policy_pass_fail_rate",
        "law_valid_rate",
        "low_sample_warning",
        "assumptions",
        "pack_id",
        "content_hash",
        "mode_label",
        "real_executable_edge_bps",
    ):
        assert hasattr(report, field), f"missing §4.4 field: {field}"

    # The assumptions block is EXPLICIT, not implied.
    assert report.assumptions.slippage_bps == 0
    assert report.assumptions.costs_bps == 0
    assert report.assumptions.quote_freshness_s is None
    assert report.assumptions.execution_mode == "paper"


@pytest.mark.parametrize(
    ("source_mode", "execution_mode", "label"),
    [
        ("replay", None, "Replay"),
        ("replay", "paper", "Backtest"),
        ("live", "paper", "Live Paper"),
        ("live", "dry_run", "Dry Run"),
        ("live", "live_guarded", "Live Guarded"),
    ],
)
def test_mode_ladder_labels_never_lie(source_mode: str, execution_mode: str | None, label: str) -> None:
    assert mode_ladder_label(source_mode, execution_mode) == label


def test_mode_ladder_rejects_unknown_combo() -> None:
    """An unmapped (source, execution) pair CRASHES — a mislabel is never silently emitted."""
    with pytest.raises(ValueError):
        mode_ladder_label("replay", "live_guarded")


def test_backtest_label_is_never_live() -> None:
    """The backtest source×execution can only ever read as 'Backtest' — never any Live label."""
    label = mode_ladder_label("replay", "paper")
    assert label == "Backtest"
    assert "Live" not in label


def test_low_sample_warning_does_not_mutate_ranking_or_means() -> None:
    """The low-sample warning is ADDITIVE: it never reorders or alters the scored metric stack."""
    run = finished_run_result(source_mode="replay")
    baseline = score_run(run)

    report = build_backtest_report(
        run,
        window=_window(),
        pack_id="p",
        content_hash="h",
        source_mode="replay",
        execution_mode="paper",
        policy_envelope=None,
    )

    # The report carries the UNTOUCHED score_run stack — same order, same means.
    assert report.leaderboard == baseline
    # And avg_clv is the honest pooled scored-CLV mean, computed independently of the warning.
    assert report.avg_clv is not None


def test_policy_pass_fail_rate_is_null_without_real_policy_eval() -> None:
    """Codex M3: the policy-named field stays None until a real envelope evaluation backs it.

    The backtest lane never runs the execution policy envelope, so law-validity is exposed under its
    honest name (``law_valid_rate``) — never under the policy name. Passing an envelope only feeds
    ``config_hash``; it does NOT populate ``policy_pass_fail_rate``.
    """
    run = finished_run_result(source_mode="replay")
    report = build_backtest_report(
        run,
        window=_window(),
        pack_id="p",
        content_hash="h",
        source_mode="replay",
        execution_mode="paper",
        policy_envelope=None,
    )

    assert report.policy_pass_fail_rate is None
    # The law-acceptance rate is still reported — under an honest, non-policy name.
    total = report.sample_size
    expected = (report.valid_count / total) if total else 0.0
    assert report.law_valid_rate == expected


# ---------------------------------------------------------------------------------------------
# CLV confidence keys off the SCORED sample, not law-valid WAITs (honesty — no overclaim).
# A run can abstain (valid WAIT) on thousands of decisions and score ZERO picks; the CLV
# confidence must then read "low", never "high" — it reflects scored-CLV coverage, not acceptance.
# ---------------------------------------------------------------------------------------------


def _run_with_rows(rows: list[dict]) -> RunResult:
    """A sealed RunResult wrapping hand-built score_rows (report path reads only these + lineage)."""
    return RunResult(
        run_id="r_clv_conf",
        source_mode="replay",
        agent_ids=["a"],
        run_events=[],
        score_rows=rows,
        evidence_hash="h",
        proof_mode_map={"a": "reproducible"},
    )


def _wait_row(tick_seq: int) -> dict:
    """A law-VALID abstention: valid is True but clv_bps is the ``"pending"`` sentinel (not scored)."""
    return {"agent_id": "a", "tick_seq": tick_seq, "valid": True, "clv_bps": "pending"}


def _scored_row(tick_seq: int, clv_bps: int) -> dict:
    """A scored pick: valid is True AND clv_bps is a real int (contributes to CLV coverage)."""
    return {"agent_id": "a", "tick_seq": tick_seq, "valid": True, "clv_bps": clv_bps}


def _report(run: RunResult) -> BacktestReport:
    return build_backtest_report(
        run,
        window=_window(),
        pack_id="p",
        content_hash="h",
        source_mode="replay",
        execution_mode="paper",
        policy_envelope=None,
    )


def test_clv_confidence_keys_off_scored_picks_not_valid_waits() -> None:
    """A zero-scored-pick run (all valid WAITs) reads LOW confidence — NEVER 'high' (no overclaim).

    30 law-valid abstentions would map to 'high' if confidence keyed off ``valid_count``; it must
    instead reflect the SCORED-CLV sample (0 picks → low). The law-valid rate stays exposed.
    """
    report = _report(_run_with_rows([_wait_row(i) for i in range(30)]))

    assert report.valid_count == 30  # law-acceptance is still reported (a distinct, honest metric)
    assert report.clv_distribution.count == 0  # nothing was scored
    assert report.avg_clv is None
    assert report.clv_confidence == "low"  # the CLV CONFIDENCE reflects scored coverage, not WAITs
    assert report.low_sample_warning is not None


def test_clv_confidence_boundary_is_on_scored_count_not_valid_count() -> None:
    """The tier boundary keys off the SCORED count (9 → low, 10 → medium) regardless of valid_count.

    Both runs carry 100 valid WAITs (so ``valid_count`` alone would say 'high'); only the number of
    scored picks moves the tier — pinning the LOW_SAMPLE_MAX=9 boundary against the scored sample.
    """
    waits = [_wait_row(i) for i in range(100)]
    nine_scored = [_scored_row(1000 + i, 12) for i in range(9)]
    ten_scored = [_scored_row(1000 + i, 12) for i in range(10)]

    low = _report(_run_with_rows(waits + nine_scored))
    medium = _report(_run_with_rows(waits + ten_scored))

    assert low.valid_count == 109 and low.clv_distribution.count == 9
    assert low.clv_confidence == "low"
    assert medium.valid_count == 110 and medium.clv_distribution.count == 10
    assert medium.clv_confidence == "medium"


def test_no_real_executable_edge_on_paper_venue() -> None:
    """The fake/paper venue never populates a real-executable-edge field (Codex M2)."""
    run = finished_run_result(source_mode="replay")
    report = build_backtest_report(
        run,
        window=_window(),
        pack_id="p",
        content_hash="h",
        source_mode="replay",
        execution_mode="paper",
        policy_envelope=None,
    )
    assert report.real_executable_edge_bps is None
