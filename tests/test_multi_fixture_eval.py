"""M7 (S6) — predeclared multi-fixture evaluation + the results producer (Tasks 19 / 19b).

The S6 evaluation is DECLARED BEFORE it is run (CON-008): a committed ``EvalProtocol`` names the
fixtures, the strategy roster, the window/close semantics, and the baselines; ``run_multi_fixture_evaluation``
then reports whatever that one pass yields — it never synthesizes a protocol at runtime. Two honesty
gates live here:

  * StaleLine is admitted ONLY when the recorded quote cadence actually backs sub-minute freshness
    (AC-009): ``stale_line_included`` is ``True`` iff the protocol asked for it AND ``cadence_ok``.
  * Every reported metric carries a machine-readable evidence rung (one of the five
    :class:`~veridex.provenance.EvidenceRung` labels) — no metric is surfaced without its provenance.
"""

from __future__ import annotations

from veridex.backtest.evaluation import EvalProtocol, run_multi_fixture_evaluation
from veridex.provenance import EvidenceRung

_RUNG_LABELS = {rung.value for rung in EvidenceRung}


def _proto(**overrides) -> EvalProtocol:
    defaults = dict(
        protocol_id="eval-m7",
        fixture_ids=[5],
        strategy_configs=["cumulative-drift"],
        window="w_eval",
        close_semantics="pre_match",
        baselines=["no_trade"],
        committed_at="2026-07-01T00:00:00Z",
    )
    defaults.update(overrides)
    return EvalProtocol(**defaults)


def _row(*, fixture_id: int = 5, kind: str = "cumulative-drift", market: str = "1X2",
         action: str = "WAIT", clv_bps: int | None = None) -> dict:
    return {"fixture_id": fixture_id, "kind": kind, "market": market, "action": action, "clv_bps": clv_bps}


# ------------------------------------------------------------------------------------------
# AC-009 — StaleLine is cadence-gated: it may be included ONLY when sub-minute cadence is proven.
# ------------------------------------------------------------------------------------------


def test_stale_line_excluded_when_cadence_insufficient() -> None:
    proto = _proto(strategy_configs=["cumulative-drift", "stale-line"])
    out = run_multi_fixture_evaluation(proto, results_by_fixture={5: [_row()]}, cadence_ok=False)
    assert out["stale_line_included"] is False


def test_stale_line_allowed_only_when_cadence_ok() -> None:
    proto = _proto(strategy_configs=["cumulative-drift", "stale-line"])
    out = run_multi_fixture_evaluation(proto, results_by_fixture={5: [_row()]}, cadence_ok=True)
    assert out["stale_line_included"] is True

    # ...and a protocol that never asked for stale-line stays excluded even with good cadence.
    no_stale = _proto(strategy_configs=["cumulative-drift"])
    out2 = run_multi_fixture_evaluation(no_stale, results_by_fixture={5: [_row()]}, cadence_ok=True)
    assert out2["stale_line_included"] is False


# ------------------------------------------------------------------------------------------
# Every reported metric carries a machine-readable evidence rung (one of the five labels).
# ------------------------------------------------------------------------------------------


def test_every_metric_carries_an_evidence_rung() -> None:
    out = run_multi_fixture_evaluation(_proto(), results_by_fixture={5: [_row()]}, cadence_ok=True)
    assert out["per_metric_rung"], "at least one metric must be reported"
    assert all(rung in _RUNG_LABELS for rung in out["per_metric_rung"].values())


# ------------------------------------------------------------------------------------------
# Baselines named in the protocol are surfaced as the zero-edge comparison floor.
# ------------------------------------------------------------------------------------------


def test_baselines_are_included() -> None:
    proto = _proto(baselines=["no_trade", "favorite"])
    out = run_multi_fixture_evaluation(proto, results_by_fixture={5: [_row()]}, cadence_ok=True)
    assert out["baselines_included"] == ["no_trade", "favorite"]


# ------------------------------------------------------------------------------------------
# Nulls (no-CLV rows) and abstentions (WAIT rows) are counted honestly — never dropped or zeroed.
# ------------------------------------------------------------------------------------------


def test_nulls_and_abstentions_are_counted_honestly() -> None:
    results_by_fixture = {
        5: [
            _row(action="FOLLOW_MOMENTUM", clv_bps=50),   # scored: neither null nor abstention
            _row(action="FOLLOW_MOMENTUM", clv_bps=None),  # null #1 (fired, but no closing CLV yet)
            _row(action="WAIT", clv_bps=None),             # null #2 AND abstention #1
        ]
    }
    out = run_multi_fixture_evaluation(_proto(), results_by_fixture=results_by_fixture, cadence_ok=True)
    assert out["nulls"] == 2
    assert out["abstentions"] == 1
