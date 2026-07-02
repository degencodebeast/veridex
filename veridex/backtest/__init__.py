"""Veridex backtest lane (T15): replay a ReplayPack through the live core → honest BacktestReport.

Proof-first, NOT a generic eval engine (GUD-2D-301): ``CompetitionRun`` is the runner and the
report is a pure projection of the sealed ``RunResult`` (SEC-003). Mode labels never lie (REQ-2D-304).
"""

from __future__ import annotations

from veridex.backtest.report import (
    BacktestAssumptions,
    BacktestReport,
    ClvDistribution,
    ThresholdSensitivityPoint,
    build_backtest_report,
    mode_ladder_label,
)
from veridex.backtest.runner import run_backtest

__all__ = [
    "BacktestAssumptions",
    "BacktestReport",
    "ClvDistribution",
    "ThresholdSensitivityPoint",
    "build_backtest_report",
    "mode_ladder_label",
    "run_backtest",
]
