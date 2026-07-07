"""Odds input-proof stamp ("verify-before-seal", input-proof axis) — offline, mocked validator.

Covers the ``classify_proof`` verdict mapping (proven / boundary / absent / error), the aggregate
counts, the FAIL-OPEN contract (one message raising ⇒ that message is ``error``, the run is never
voided), and the report-only attach (no leaderboard/hash perturbation, SEC-005). Nothing hits the
network — the async validator is injected.
"""

from __future__ import annotations

from typing import Any

from tests.test_backtest_report import _report, _run_with_rows, _scored_row
from veridex.ingest.odds_proof import (
    ABSENT,
    BOUNDARY,
    ERROR,
    PROVEN,
    OddsProofSummary,
    attach_odds_proof_status,
    classify_proof,
    summarize_odds_proofs,
)


def _node(right: bool = False) -> dict[str, Any]:
    """A minimal Merkle proof node ``{hash:[32 uint8], isRightSibling:bool}``."""
    return {"hash": [0] * 32, "isRightSibling": right}


def _proven_resp() -> dict[str, Any]:
    """Non-empty subTree AND mainTree ⇒ a full two-tier inclusion proof."""
    return {
        "odds": {},
        "summary": {},
        "subTreeProof": [_node(), _node(True), _node(), _node()],
        "mainTreeProof": [_node() for _ in range(6)],
    }


def _boundary_resp() -> dict[str, Any]:
    """Empty subTree, non-empty mainTree ⇒ boundary (first pre-match record)."""
    return {"odds": {}, "summary": {}, "subTreeProof": [], "mainTreeProof": [_node() for _ in range(6)]}


# --- classify_proof verdict mapping ------------------------------------------


def test_classify_proven() -> None:
    assert classify_proof(_proven_resp()) == PROVEN


def test_classify_boundary() -> None:
    assert classify_proof(_boundary_resp()) == BOUNDARY


def test_classify_absent_when_both_empty_or_missing() -> None:
    assert classify_proof({"subTreeProof": [], "mainTreeProof": []}) == ABSENT
    assert classify_proof({"odds": {}, "summary": {}}) == ABSENT  # both missing
    assert classify_proof(None) == ABSENT  # type: ignore[arg-type]


def test_classify_absent_on_404_sentinel() -> None:
    assert classify_proof({"_status": 404}) == ABSENT
    assert classify_proof({"_not_found": True}) == ABSENT


def test_classify_error_on_error_sentinel_or_bad_shape() -> None:
    assert classify_proof({"_error": "timeout"}) == ERROR
    assert classify_proof("not-a-dict") == ERROR  # type: ignore[arg-type]


# --- aggregate counts + per-message breakdown --------------------------------


async def test_summarize_counts_each_status() -> None:
    responses = {
        ("m_proven", 1): _proven_resp(),
        ("m_boundary", 2): _boundary_resp(),
        ("m_absent", 3): {"subTreeProof": [], "mainTreeProof": []},
    }
    calls: list[tuple[str, int]] = []

    async def fake_validate(message_id: str, ts: int, **_kw: Any) -> dict[str, Any]:
        calls.append((message_id, ts))
        return responses[(message_id, ts)]

    summary = await summarize_odds_proofs(responses.keys(), validate=fake_validate)

    assert isinstance(summary, OddsProofSummary)
    # validate_odds was called once per message with (message_id, ts).
    assert sorted(calls) == sorted(responses.keys())
    assert summary.n_total == 3
    assert summary.n_proven == 1
    assert summary.n_boundary == 1
    assert summary.n_absent == 1
    assert summary.n_error == 0
    by_id = {p.message_id: p.status for p in summary.per_message}
    assert by_id == {"m_proven": PROVEN, "m_boundary": BOUNDARY, "m_absent": ABSENT}


async def test_summarize_is_fail_open_on_exception() -> None:
    """One message raising ⇒ that message is ``error``; the aggregate never raises / never voids."""

    async def flaky_validate(message_id: str, ts: int, **_kw: Any) -> dict[str, Any]:
        if message_id == "m_boom":
            raise RuntimeError("transport blew up")
        return _proven_resp()

    summary = await summarize_odds_proofs(
        [("m_ok", 1), ("m_boom", 2)], validate=flaky_validate
    )

    assert summary.n_total == 2
    assert summary.n_proven == 1
    assert summary.n_error == 1
    by_id = {p.message_id: p.status for p in summary.per_message}
    assert by_id == {"m_ok": PROVEN, "m_boom": ERROR}


async def test_summarize_empty_input() -> None:
    async def never(*_a: Any, **_kw: Any) -> dict[str, Any]:  # pragma: no cover - must not be called
        raise AssertionError("validate should not be called for empty input")

    summary = await summarize_odds_proofs([], validate=never)
    assert summary.n_total == 0
    assert summary.as_dict()["per_message"] == []


# --- honesty label + report-only attach (SEC-005) ----------------------------


def test_claim_is_honest_and_not_overclaiming() -> None:
    summary = OddsProofSummary(
        n_total=3, n_proven=2, n_boundary=1, n_absent=0, n_error=0, per_message=[]
    )
    claim = summary.claim()
    assert claim == "TxLINE returned valid Merkle inclusion proofs for 2/3 odds messages"
    lowered = claim.lower()
    for overclaim in ("independently verified", "trustless", "on-chain root", "verified against"):
        assert overclaim not in lowered


def test_attach_is_report_only_and_never_ranked() -> None:
    """Attaching the stamp lands on a declared field, leaves the leaderboard + config_hash alone."""
    base = _report(_run_with_rows([_scored_row(0, 10)]))
    summary = OddsProofSummary(
        n_total=1,
        n_proven=1,
        n_boundary=0,
        n_absent=0,
        n_error=0,
        per_message=[],
    )

    report = attach_odds_proof_status(base, summary)

    dumped = report.model_dump()
    assert dumped["odds_proof_status"]["n_proven"] == 1
    assert dumped["odds_proof_status"]["claim"].startswith("TxLINE returned valid Merkle")
    # Report-only: the reproducibility fingerprint is untouched (no hash perturbation).
    assert report.config_hash == base.config_hash
    assert report.evidence_hash == base.evidence_hash
    # SEC-005: invisible to every ranked leaderboard row.
    for row in report.leaderboard:
        assert "odds_proof_status" not in row
