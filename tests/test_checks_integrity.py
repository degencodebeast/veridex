"""WD-5a — checks integrity: evidence_integrity recomputes hash; llm_boundary runs real audit.

RED → GREEN tests written strictly before the implementation fix. Both checks in
``read_path_check_block`` were no-ops prior to this fix:

  * ``evidence_integrity`` only asserted non-empty hash — never recomputed, so a tampered
    run with a stale hash still showed ✅.
  * ``llm_boundary`` was hardcoded ``"pass"`` — the real import audit never ran.

TDD discipline: RED output is recorded in the commit message. Tests are written here and
watched fail (wrong verdict), then the minimal implementation turns them GREEN.
"""

from __future__ import annotations

import copy
import json

import pytest

from tests._arena_fixtures import finished_run_result
from veridex.checks.build import build_performance_metrics
from veridex.ingest.marketstate import MarketState
from veridex.runtime.competition import read_path_check_block
from veridex.runtime.evidence import compute_evidence_hash, serialize_payload
from veridex.runtime.orchestrator import Agent, CompetitionRun
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.runtime.window import RunWindow
from veridex.scoring import score_run

# ---------------------------------------------------------------------------
# evidence_integrity — fail-closed when run_events diverge from evidence_hash
# ---------------------------------------------------------------------------


def test_evidence_integrity_fails_on_tampered_run() -> None:
    """read_path_check_block must return ``"fail"`` when run_events no longer match evidence_hash.

    Tamper: inject a new key into ``run_events[0]`` so the recomputed SHA-256 diverges from
    the stored ``evidence_hash`` (which was sealed over the original events).

    RED: before WD-5a the check only tested ``bool(run.evidence_hash)`` so it returned
    ``"pass"`` on a tampered run. After WD-5a it recomputes and fails-closed.
    """
    run = finished_run_result()
    scores = score_run(run)

    # Tamper: mutate a dict inside the list — frozen dataclass can't stop this.
    # The stored evidence_hash is now stale (sealed before the mutation).
    run.run_events[0]["_tampered"] = "injected_by_test"

    checks = read_path_check_block(scores, run)
    assert checks["evidence_integrity"]["result"] == "fail"


def test_evidence_integrity_passes_on_clean_run() -> None:
    """read_path_check_block must return ``"pass"`` on an unmodified (clean) run.

    The recomputed hash must equal the stored evidence_hash for a run that was
    not tampered with after sealing.
    """
    run = finished_run_result()
    scores = score_run(run)

    checks = read_path_check_block(scores, run)
    assert checks["evidence_integrity"]["result"] == "pass"


def test_evidence_integrity_exposes_recomputed_match_flag() -> None:
    """The ``recomputed_match`` boolean surfaces the comparison result on the card."""
    run = finished_run_result()
    scores = score_run(run)

    # Clean run: recomputed_match must be True (now nested under details — typed CheckResult).
    checks = read_path_check_block(scores, run)
    assert checks["evidence_integrity"]["details"]["recomputed_match"] is True

    # Tampered run: recomputed_match must be False.
    run2 = finished_run_result()
    scores2 = score_run(run2)
    run2.run_events[0]["_tampered"] = "injected_by_test"
    checks2 = read_path_check_block(scores2, run2)
    assert checks2["evidence_integrity"]["details"]["recomputed_match"] is False


# ---------------------------------------------------------------------------
# llm_boundary — runs the real import audit; catches violations; fail-closed
# ---------------------------------------------------------------------------


def test_llm_boundary_fails_when_audit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_path_check_block must return ``"fail"`` when ``assert_no_llm_imports`` raises.

    Monkeypatches the function in the ``checks.build`` namespace (where ``read_path_check_block`` now
    delegates the audit) so the try/except inside ``_llm_boundary`` catches the injected
    ``AssertionError``.

    RED: before WD-5a the check was hardcoded ``"pass"``, ignoring any exception entirely.
    After WD-5a/WD-5b it runs the real audit (in ``checks/build.py``) and catches violations.
    """
    import veridex.checks.build as build_mod

    def _always_raise(path: object) -> None:
        raise AssertionError("Forbidden LLM import 'openai' in fake.py (injected by test)")

    monkeypatch.setattr(build_mod, "assert_no_llm_imports", _always_raise)

    run = finished_run_result()
    scores = score_run(run)

    checks = read_path_check_block(scores, run)
    assert checks["llm_boundary"]["result"] == "fail"


def test_llm_boundary_passes_on_clean_trust_path() -> None:
    """read_path_check_block must return ``"pass"`` when the real trust path is LLM-SDK-free.

    Exercises the real ``assert_no_llm_imports`` over the real trust-path targets — this
    is the live integration smoke-test that proves the boundary is actually enforced.
    """
    run = finished_run_result()
    scores = score_run(run)

    checks = read_path_check_block(scores, run)
    assert checks["llm_boundary"]["result"] == "pass"


# ---------------------------------------------------------------------------
# Fail-closed: malformed evidence must not crash card-build (WD-5a codex review)
# ---------------------------------------------------------------------------


def test_evidence_integrity_fails_closed_on_duplicate_sequence_no() -> None:
    """Duplicate sequence_no raises ValueError inside compute_evidence_hash.

    The card must absorb it and return result=``"fail"`` with recomputed_match=False
    and a populated ``error`` field — never propagate the exception.

    RED: before this fix compute_evidence_hash raises and card-build crashes.
    """
    run = finished_run_result()
    scores = score_run(run)

    # Inject a second copy of the first event — same sequence_no, duplicate.
    run.run_events.append(dict(run.run_events[0]))

    checks = read_path_check_block(scores, run)
    assert checks["evidence_integrity"]["result"] == "fail"
    assert checks["evidence_integrity"]["details"]["recomputed_match"] is False
    assert checks["evidence_integrity"]["error"] is not None


def test_evidence_integrity_fails_closed_on_missing_sequence_no() -> None:
    """Missing sequence_no raises KeyError inside compute_evidence_hash sort key.

    The card must absorb it and return result=``"fail"``.

    RED: before this fix the KeyError propagates and card-build crashes.
    """
    run = finished_run_result()
    scores = score_run(run)

    # Remove the sequence_no key from the first event so the sort-key lambda KeyErrors.
    del run.run_events[0]["sequence_no"]

    checks = read_path_check_block(scores, run)
    assert checks["evidence_integrity"]["result"] == "fail"
    assert checks["evidence_integrity"]["details"]["recomputed_match"] is False


def test_llm_boundary_fails_closed_on_non_assertion_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """assert_no_llm_imports may also raise SyntaxError/OSError on malformed source.

    The narrow ``except AssertionError`` lets those propagate; the card must
    treat any audit exception as a boundary failure, not a crash.

    RED: before this fix a SyntaxError escapes the except clause and crashes card-build.
    """
    import veridex.checks.build as build_mod

    def _raise_syntax_error(path: object) -> None:
        raise SyntaxError("Fake syntax error in trust-path file (injected by test)")

    monkeypatch.setattr(build_mod, "assert_no_llm_imports", _raise_syntax_error)

    run = finished_run_result()
    scores = score_run(run)

    checks = read_path_check_block(scores, run)
    assert checks["llm_boundary"]["result"] == "fail"


# ---------------------------------------------------------------------------
# WD-5b — SEC-001 shape (7 CheckIds, CLV NOT a check) + evidence boundary
# ---------------------------------------------------------------------------


def test_read_path_check_block_emits_seven_check_ids_and_no_clv() -> None:
    """SEC-001: ``read_path_check_block`` emits exactly the 7 frozen CheckIds; CLV is NOT one of them.

    The 2-arg convenience path has no manifest/anchor/events, so the manifest/policy/receipt
    checks are ``not_applicable`` and ANCHOR follows the run's source_mode (replay→na).
    """
    run = finished_run_result()
    scores = score_run(run)

    checks = read_path_check_block(scores, run)
    assert set(checks) == {
        "evidence_integrity",
        "llm_boundary",
        "metrics_recomputed",
        "manifest_bound",
        "policy_obeyed",
        "receipt_separation",
        "anchor",
    }
    assert "clv" not in checks  # SEC-001: CLV is a performance metric, never a check
    # CLV lives in the separate Performance-Metrics block instead.
    assert "clv" in build_performance_metrics(scores)


def test_read_path_check_block_does_not_mutate_sealed_evidence() -> None:
    """The migration changes the proof-card representation, NOT the sealed evidence.

    Building checks + metrics must leave ``run.run_events`` and ``run.evidence_hash``
    byte-identical, and the sealed prefix must still recompute to the same hash.
    """
    run = finished_run_result()
    scores = score_run(run)

    evidence_hash_before = run.evidence_hash
    events_before = copy.deepcopy(run.run_events)

    read_path_check_block(scores, run)
    build_performance_metrics(scores)

    assert run.evidence_hash == evidence_hash_before
    assert run.run_events == events_before
    # The sealed RunEvent prefix still recomputes to the identical evidence_hash.
    assert compute_evidence_hash(run.run_events) == evidence_hash_before


# ---------------------------------------------------------------------------
# METRICS_RECOMPUTED — falsifiable (SEC-002): a tampered persisted clv_bps fails
# ---------------------------------------------------------------------------


def _first_scored_index(run: object) -> int:
    """Index of the first genuinely-scored row (valid + numeric clv_bps, not a bool/sentinel)."""
    for i, r in enumerate(run.score_rows):  # type: ignore[attr-defined]
        clv = r.get("clv_bps")
        if r.get("valid") is True and isinstance(clv, int) and not isinstance(clv, bool):
            return i
    raise AssertionError("fixture has no scored row to tamper")


def test_metrics_recomputed_passes_on_untampered_run() -> None:
    """The honest case: the displayed score table re-derives faithfully from sealed evidence."""
    run = finished_run_result()
    scores = score_run(run)
    assert read_path_check_block(scores, run)["metrics_recomputed"]["result"] == "pass"


def test_metrics_recomputed_fails_on_tampered_persisted_clv() -> None:
    """SEC-002 falsifiability: tampering a persisted ``clv_bps`` in ``run.score_rows`` (the value
    that feeds the displayed leaderboard avg) must flip METRICS_RECOMPUTED to ``fail``.

    The fresh recompute re-derives clv from the SEALED RunEvents (market snapshots + the sealed
    decision action) via the deterministic law — NOT from the stored clv — so a doctored score row
    no longer matches. ``score_rows`` is NOT part of the hashed evidence prefix, so
    ``evidence_integrity`` still passes; only METRICS_RECOMPUTED catches this tamper class.
    """
    run = finished_run_result()
    idx = _first_scored_index(run)
    run.score_rows[idx]["clv_bps"] = run.score_rows[idx]["clv_bps"] + 9999  # doctor the displayed metric

    # The displayed aggregate now reflects the tamper (score_run reads score_rows).
    tampered_scores = score_run(run)
    checks = read_path_check_block(tampered_scores, run)

    assert checks["metrics_recomputed"]["result"] == "fail"
    assert checks["metrics_recomputed"]["error"] is not None
    # the per-row discrepancy is surfaced in rules (persisted vs recomputed).
    assert any("recomputed_clv_bps" in rule for rule in checks["metrics_recomputed"]["rules"])
    # the tamper did NOT touch the hashed evidence prefix, so evidence_integrity stays pass.
    assert checks["evidence_integrity"]["result"] == "pass"


def test_metrics_recomputed_fails_on_coordinated_score_row_tamper() -> None:
    """SEC-002 (coordinated-tamper evasion closed): editing BOTH ``clv_bps`` AND ``raw_action`` in a
    score row so the row is INTERNALLY CONSISTENT must STILL flip METRICS_RECOMPUTED to ``fail``.

    The tamper swaps a scored row to look like a WAIT (``raw_action`` = WAIT, ``clv_bps`` = the
    ``"pending"`` sentinel a WAIT recompute yields) — self-consistent, so a check that sourced the
    action from ``score_rows`` would be fooled into ``pass``. Because the action is instead read from
    the SEALED ``decision`` event (``action_payload_json``, the original FLAG_VALUE), the recompute
    still yields the real numeric clv ≠ ``"pending"`` ⇒ ``fail``. The sealed RunEvents are untouched,
    so ``evidence_integrity`` stays ``pass``.
    """
    run = finished_run_result()
    idx = _first_scored_index(run)
    row = run.score_rows[idx]
    # Coordinated, internally-consistent doctor of the NON-hashed score row:
    row["clv_bps"] = "pending"  # what a WAIT recompute would produce
    row["raw_prescore"]["raw_action"] = {"type": "WAIT", "params": {}}

    checks = read_path_check_block(score_run(run), run)

    assert checks["metrics_recomputed"]["result"] == "fail"
    assert checks["metrics_recomputed"]["error"] is not None
    # the recompute used the SEALED action (numeric clv), not the row's WAIT/"pending".
    assert any(rule.get("persisted_clv_bps") == "pending" for rule in checks["metrics_recomputed"]["rules"])
    assert checks["evidence_integrity"]["result"] == "pass"


def test_metrics_recomputed_not_applicable_when_no_score_rows() -> None:
    """A run with no score rows is an honest ``not_applicable`` (nothing to recompute), never a
    vacuous ``pass`` — matching the POLICY_OBEYED / RECEIPT_SEPARATION honesty pattern."""
    run = finished_run_result()
    run.score_rows.clear()  # mutate the list in place (frozen dataclass guards reassignment only)

    checks = read_path_check_block(score_run(run), run)
    assert checks["metrics_recomputed"]["result"] == "not_applicable"


# ---------------------------------------------------------------------------
# REQ-2D-801 — METRICS_RECOMPUTED honors T7's windowed row shapes:
#   * window_clv_bps rows (fixed_duration / manual_stop): CLV stored under window_clv_bps,
#     clv_bps ABSENT — must verify against window_clv_bps (anti-tamper preserved).
#   * pending_horizon rows: honest abstention (clv_bps == "pending") — skip like WAIT.
# These runs are built through the REAL orchestrator (finalize(window=...)) so the row shapes
# are byte-identical to production, not hand-mocked.
# ---------------------------------------------------------------------------

_WKEY = "OU_2_5"


def _wmarket(prob_bps: dict[str, int]) -> dict:
    return {"stable_prob_bps": dict(prob_bps), "stable_price": {"over": 1.6, "under": 2.4}, "suspended": False}


def _wms(prob_bps: dict[str, int], *, tick_seq: int, ts: int) -> MarketState:
    return MarketState(fixture_id=1, tick_seq=tick_seq, ts=ts, phase=2, markets={_WKEY: _wmarket(prob_bps)}, scores={})


def _wflag_agent(agent_id: str = "flagger") -> Agent:
    """An agent that always FLAG_VALUEs 'over' — a scored, numeric-CLV action every tick."""

    async def decide(market_state: MarketState) -> AgentAction:
        return AgentAction(type=SportsActionType.FLAG_VALUE, params={"market_key": _WKEY, "side": "over"})

    return Agent(agent_id=agent_id, proof_mode="reproducible", decide=decide)


def _window(end_rule: str, **kw: object) -> RunWindow:
    base: dict[str, object] = {"window_id": "win-1", "fixture_id": 1, "market_allowlist": ["OU"], "end_rule": end_rule}
    base.update(kw)
    return RunWindow(**base)  # type: ignore[arg-type]


async def _fixed_duration_run() -> object:
    """A fixed_duration windowed run: tick0 is a scored WINDOW-CLV row (window_clv_bps=300, no
    clv_bps); tick1 (entry AT close) is a pending_horizon abstention (clv_bps=="pending")."""
    run = CompetitionRun([_wflag_agent()], source_mode="replay", run_id="wc-check")
    await run.feed(_wms({"over": 6000}, tick_seq=0, ts=1000))
    await run.feed(_wms({"over": 6300}, tick_seq=1, ts=1100))  # window_end_ts = 1100
    return await run.finalize(window=_window("fixed_duration", duration_s=100, min_clv_horizon_s=10))


async def test_metrics_recomputed_passes_on_honest_window_clv() -> None:
    """RED before the fix: an honest fixed_duration run stores CLV under ``window_clv_bps`` (clv_bps
    absent). The old check compared ``redo["clv_bps"]`` (numeric) to ``row.get("clv_bps")`` (None) and
    spuriously FAILED an honest run. The fix verifies against ``window_clv_bps`` ⇒ ``pass``."""
    result = await _fixed_duration_run()

    # Confirm the fixture really carries the window_clv_bps shape the check must understand.
    scored = {r["tick_seq"]: r for r in result.score_rows}[0]  # type: ignore[attr-defined]
    assert scored["window_clv_bps"] == 300
    assert "clv_bps" not in scored

    checks = read_path_check_block(score_run(result), result)
    assert checks["metrics_recomputed"]["result"] == "pass"


async def test_metrics_recomputed_passes_on_honest_pending_horizon() -> None:
    """RED before the fix: a pending_horizon row displays the ``"pending"`` sentinel (an abstention,
    like WAIT), but the sealed-evidence recompute yields a NUMERIC clv (the horizon decision lives in
    finalize, not the law) — so the old check spuriously FAILED. The fix skips it ⇒ ``pass``."""
    run = CompetitionRun([_wflag_agent()], source_mode="replay", run_id="ph-check")
    await run.feed(_wms({"over": 6000}, tick_seq=0, ts=1000))  # far from close -> scored true CLV
    await run.feed(_wms({"over": 6300}, tick_seq=1, ts=1970))  # 30s before close -> pending_horizon
    await run.feed_closing(_wms({"over": 6600}, tick_seq=2, ts=2000))  # window_end_ts = 2000
    result = await run.finalize(window=_window("pre_match", min_clv_horizon_s=60))

    # Confirm the pending_horizon shape the check must treat as an honest abstention.
    horizoned = {r["tick_seq"]: r for r in result.score_rows}[1]
    assert horizoned["reason"] == "pending_horizon"
    assert horizoned["clv_bps"] == "pending"

    checks = read_path_check_block(score_run(result), result)
    assert checks["metrics_recomputed"]["result"] == "pass"


async def test_metrics_recomputed_fails_on_tampered_window_clv() -> None:
    """Anti-tamper preserved for window rows: the SAME honest run passes, but doctoring the displayed
    ``window_clv_bps`` away from the recomputed value must STILL flip METRICS_RECOMPUTED to ``fail``.

    This proves the fix does NOT "pass" window rows by skipping them — it verifies the recompute
    against ``window_clv_bps``, so a tampered window CLV is caught exactly like a tampered true CLV."""
    result = await _fixed_duration_run()

    # Baseline: the honest run passes (so a subsequent fail is attributable ONLY to the tamper).
    assert read_path_check_block(score_run(result), result)["metrics_recomputed"]["result"] == "pass"

    scored = {r["tick_seq"]: r for r in result.score_rows}[0]  # type: ignore[attr-defined]
    scored["window_clv_bps"] = scored["window_clv_bps"] + 9999  # doctor the displayed WINDOW metric

    checks = read_path_check_block(score_run(result), result)
    assert checks["metrics_recomputed"]["result"] == "fail"
    assert checks["metrics_recomputed"]["error"] is not None
    # The per-row discrepancy surfaces the doctored displayed value vs the sealed-evidence recompute.
    assert any(rule.get("persisted_clv_bps") == 300 + 9999 for rule in checks["metrics_recomputed"]["rules"])
    # The tamper touched only the non-hashed score_rows, so the evidence prefix still verifies.
    assert checks["evidence_integrity"]["result"] == "pass"


# ---------------------------------------------------------------------------
# T8c — seal the window config + VERIFY pending_horizon from SEALED evidence.
#
# The relabel evasion (the whole point): score_rows are NOT evidence-hashed, so a coordinated
# tamper — relabel a genuinely-SCORED losing row to reason="pending_horizon" + clv_bps="pending" —
# hides it from the CLV mean while EVIDENCE_INTEGRITY still passes. Before T8c the check TRUSTED the
# label and skipped it (evasion succeeds). After T8c the check re-derives is_pending_horizon from the
# SEALED window config + the row's sealed entry tick and FAILS a row that does not genuinely qualify.
# These runs go through the REAL orchestrator (finalize(window=...)) so the sealed shapes are
# byte-identical to production.
# ---------------------------------------------------------------------------

_WINDOW_CONFIG_EVENT = "window_config"


async def _evasion_run() -> object:
    """A pre_match run with ONE genuinely-scored losing row FAR from close (NOT pending_horizon).

    tick0 (ts=1000) is 1000s from the window close (2000) — far outside any 60s horizon — so it is a
    real scored true-CLV row. The closing line (over 5000 < entry 6000) makes it a LOSING row: exactly
    the row an attacker wants to hide from the CLV mean by relabelling it a horizon abstention.
    """
    run = CompetitionRun([_wflag_agent()], source_mode="replay", run_id="t8c-evasion")
    await run.feed(_wms({"over": 6000}, tick_seq=0, ts=1000))
    await run.feed_closing(_wms({"over": 5000}, tick_seq=1, ts=2000))  # window_end_ts = 2000
    return await run.finalize(window=_window("pre_match", min_clv_horizon_s=60))


async def test_window_config_is_sealed_into_run_events() -> None:
    """The windowed run seals end_rule + min_clv_horizon_s + the effective window_end_ts as evidence."""
    result = await _evasion_run()
    cfg_events = [e for e in result.run_events if e["event_type"] == _WINDOW_CONFIG_EVENT]  # type: ignore[attr-defined]
    assert len(cfg_events) == 1
    cfg = json.loads(cfg_events[0]["result_payload_json"])
    assert cfg["end_rule"] == "pre_match"
    assert cfg["min_clv_horizon_s"] == 60
    assert cfg["window_end_ts"] == 2000


async def test_no_window_run_seals_no_window_config() -> None:
    """A window=None run seals NO window_config event (keeps the legacy path byte-identical)."""
    run = CompetitionRun([_wflag_agent()], source_mode="replay", run_id="t8c-nowin")
    await run.feed(_wms({"over": 6000}, tick_seq=0, ts=1000))
    result = await run.finalize()  # window=None
    assert not [e for e in result.run_events if e["event_type"] == _WINDOW_CONFIG_EVENT]


async def test_sealed_window_config_is_tamper_evident() -> None:
    """Tampering the sealed window config diverges the evidence hash ⇒ EVIDENCE_INTEGRITY fails."""
    result = await _evasion_run()
    # Baseline: the clean run's sealed prefix verifies.
    assert read_path_check_block(score_run(result), result)["evidence_integrity"]["result"] == "pass"

    for e in result.run_events:  # type: ignore[attr-defined]
        if e["event_type"] == _WINDOW_CONFIG_EVENT:
            e["result_payload_json"] = serialize_payload(
                {"end_rule": "pre_match", "min_clv_horizon_s": 999_999, "window_end_ts": 2000}
            )

    checks = read_path_check_block(score_run(result), result)
    assert checks["evidence_integrity"]["result"] == "fail"


async def test_relabel_evasion_is_caught() -> None:
    """THE evasion: a genuinely-scored losing row relabelled pending_horizon+pending must FAIL.

    Before T8c METRICS_RECOMPUTED trusted the label and skipped this row (PASS — the hole). After
    T8c it re-derives is_pending_horizon(entry_ts, window_end_ts, min_clv_horizon_s) from SEALED data;
    the entry is 1000s from close (not within 60s) so the label is a LIE ⇒ FAIL.
    """
    result = await _evasion_run()
    row0 = {r["tick_seq"]: r for r in result.score_rows}[0]  # type: ignore[attr-defined]
    # Sanity: genuinely scored (numeric clv, valid) — a real row, not an abstention.
    assert row0["valid"] is True
    assert isinstance(row0["clv_bps"], int) and not isinstance(row0["clv_bps"], bool)

    # Baseline: the honest run passes, so a subsequent fail is attributable ONLY to the relabel.
    assert read_path_check_block(score_run(result), result)["metrics_recomputed"]["result"] == "pass"

    # The attack: hide the losing row from the CLV mean by relabelling it a horizon abstention.
    row0["reason"] = "pending_horizon"
    row0["clv_bps"] = "pending"

    checks = read_path_check_block(score_run(result), result)
    assert checks["metrics_recomputed"]["result"] == "fail"
    assert checks["metrics_recomputed"]["error"] is not None
    # The discrepancy is surfaced as a mislabelled-horizon row (re-derived, not trusted).
    assert any(rule.get("reason") == "pending_horizon_mislabeled" for rule in checks["metrics_recomputed"]["rules"])
    # The tamper touched only the non-hashed score_rows, so the evidence prefix still verifies.
    assert checks["evidence_integrity"]["result"] == "pass"


async def test_honest_pending_horizon_still_passes_after_t8c() -> None:
    """A row that GENUINELY qualifies (entry within the horizon) re-derives True ⇒ still passes.

    Proves T8c verifies rather than blanket-rejects: the same sealed-evidence re-derivation that
    catches the relabel confirms an honest pending_horizon row and skips it like WAIT.
    """
    run = CompetitionRun([_wflag_agent()], source_mode="replay", run_id="t8c-honest")
    await run.feed(_wms({"over": 6000}, tick_seq=0, ts=1000))  # far from close -> scored true CLV
    await run.feed(_wms({"over": 6300}, tick_seq=1, ts=1970))  # 30s before close -> genuine pending
    await run.feed_closing(_wms({"over": 6600}, tick_seq=2, ts=2000))  # window_end_ts = 2000
    result = await run.finalize(window=_window("pre_match", min_clv_horizon_s=60))

    horizoned = {r["tick_seq"]: r for r in result.score_rows}[1]
    assert horizoned["reason"] == "pending_horizon" and horizoned["clv_bps"] == "pending"

    assert read_path_check_block(score_run(result), result)["metrics_recomputed"]["result"] == "pass"
