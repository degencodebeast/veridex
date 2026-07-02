"""Phase-2B Task 6 — executor lane tests (the receipt≠skill KEYSTONE, TDD).

The lane is STRICTLY DOWNSTREAM of the sealed run + deterministic law. A venue fill is
production-readiness evidence, NEVER skill evidence:

* running the lane leaves ``score_run`` / ``leaderboard`` / ``evidence_hash`` byte-identical (AC-2B-05);
* every emitted competition event is ``evidence=False`` with non-empty ``derived_from``;
* the law-recomputed edge gates execution, never the LLM claim (AC-2B-03);
* ``dry_run`` simulates with NO real submit (AC-2B-07);
* ``REQUIRES_HUMAN`` parks an ``awaiting_human`` record and never submits;
* an ineligible agent never submits.
"""

from __future__ import annotations

import time

import pytest

from tests._arena_fixtures import _DEMO_MARKET_KEY, finished_run_result
from veridex.competition.events import EventType
from veridex.competition.models import (
    AgentEntry,
    Competition,
    CompetitionConfig,
    CompetitionStatus,
    CompetitionType,
)
from veridex.execution.runner import _FALLBACK_STAKE, _size_stake, run_execution_lane
from veridex.leaderboard import leaderboard
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.orchestrator import RunResult
from veridex.scoring import score_run
from veridex.store import InMemoryStore
from veridex.strategies.value import value_proposals
from veridex.venues.base import Quote
from veridex.venues.sx_bet import FakeVenueAdapter

# FakeVenueAdapter resolves to the venue slug "fake" (class-name derived); the envelope must
# allow it for a clean APPROVED path.
_VENUE = "fake"


def _env(**overrides: object) -> PolicyEnvelope:
    """Build a permissive envelope; override single fields per test."""
    base: dict[str, object] = {
        "max_stake": 100.0,
        "max_orders_per_run": 100,
        "max_orders_per_session": 100,
        "max_orders_per_day": 100,
        "venue_allowlist": [_VENUE],
        "market_allowlist": [_DEMO_MARKET_KEY],
        "min_edge_bps": 0,
        "max_slippage_bps": 10_000,
        "max_price": 1.0e9,
        "max_quote_age_s": 10**9,
        "cooldown_s": 0,
        "human_approval_threshold": 1.0e12,  # default: stake below -> APPROVED (not REQUIRES_HUMAN)
        "kill_switch": False,
    }
    base.update(overrides)
    return PolicyEnvelope(**base)  # type: ignore[arg-type]


@pytest.fixture
def rr() -> RunResult:
    """Build the sealed fixture run in SYNC context (the fixture uses ``asyncio.run``).

    Resolved before the async test body enters the event loop, so the nested ``asyncio.run``
    inside :func:`finished_run_result` does not collide with a running loop.
    """
    return finished_run_result()


def _comp(competition_id: str) -> Competition:
    """A minimal draft competition for the lane to attach execution records to."""
    return Competition(
        competition_id=competition_id,
        config=CompetitionConfig(
            competition_type=CompetitionType.REPLAY_ARENA,
            source_mode="replay",
            market_scope="WC:TEST",
            roster_size=2,
        ),
        status=CompetitionStatus.DRAFT,
        entries=[],
        run_id=None,
    )


def _entries(rr: RunResult, *, eligible: bool) -> dict[str, AgentEntry]:
    return {
        agent_id: AgentEntry(
            agent_id=agent_id,
            owner="u",
            strategy="value",
            model=None,
            proof_mode="reproducible",
            execution_eligibility=eligible,
        )
        for agent_id in rr.agent_ids
    }


def _eligible(rr: RunResult) -> dict[str, AgentEntry]:
    return _entries(rr, eligible=True)


def _ineligible(rr: RunResult) -> dict[str, AgentEntry]:
    return _entries(rr, eligible=False)


async def test_receipt_excluded_from_skill(rr: RunResult) -> None:  # AC-2B-05 KEYSTONE
    board_before = leaderboard([dict(r) for r in score_run(rr)])
    ev_before = rr.evidence_hash

    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    events = await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        envelope=_env(min_edge_bps=0),
        adapter=FakeVenueAdapter(fill=True),
        entries_by_agent=_eligible(rr),
        execution_mode="dry_run",
        base_seq=100,
        event_ts=0,
    )

    board_after = leaderboard([dict(r) for r in score_run(rr)])
    assert board_before == board_after  # fills NEVER change skill/leaderboard
    assert rr.evidence_hash == ev_before  # seal untouched
    assert events and all((not e.evidence) and e.derived_from for e in events)


async def test_law_gate_beats_llm_claim(rr: RunResult) -> None:  # AC-2B-03
    max_edge = max(row["recomputed_edge_bps"] for row in rr.score_rows)
    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    events = await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        envelope=_env(min_edge_bps=max_edge + 1),  # above EVERY sealed law edge
        adapter=FakeVenueAdapter(fill=True),
        entries_by_agent=_eligible(rr),
        execution_mode="dry_run",
        base_seq=0,
        event_ts=0,
    )
    assert all(e.event_type.value != "execution_submitted" for e in events)
    assert await store.list_executions("c") == []  # nothing cleared the law gate


async def test_dry_run_simulated_no_real_submit(rr: RunResult) -> None:  # AC-2B-07
    adapter = FakeVenueAdapter()
    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    events = await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        envelope=_env(min_edge_bps=0),
        adapter=adapter,
        entries_by_agent=_eligible(rr),
        execution_mode="dry_run",
        base_seq=0,
        event_ts=0,
    )
    assert adapter.submit_calls == 0
    assert any(e.event_type.value == "execution_receipt" for e in events)
    assert any(e.event_type.value == "execution_submitted" for e in events)
    # Records reached the simulated filled terminal.
    recs = await store.list_executions("c")
    assert any(r.status.value == "filled" for r in recs)


async def test_requires_human_persists_awaiting_no_submit(rr: RunResult) -> None:
    adapter = FakeVenueAdapter()
    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        envelope=_env(human_approval_threshold=0.0),  # any stake escalates
        adapter=adapter,
        entries_by_agent=_eligible(rr),
        execution_mode="live_guarded",
        base_seq=0,
        event_ts=0,
    )
    recs = await store.list_executions("c")
    assert recs
    assert any(r.status.value == "awaiting_human" for r in recs)
    assert adapter.submit_calls == 0


async def test_ineligible_agent_no_submit(rr: RunResult) -> None:
    adapter = FakeVenueAdapter()
    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    events = await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        envelope=_env(min_edge_bps=0),
        adapter=adapter,
        entries_by_agent=_ineligible(rr),
        execution_mode="dry_run",
        base_seq=0,
        event_ts=0,
    )
    assert all(e.event_type.value != "execution_submitted" for e in events)
    assert adapter.submit_calls == 0
    recs = await store.list_executions("c")
    assert recs and all(r.status.value == "rejected" for r in recs)


async def test_live_guarded_fill_submits_real_order(rr: RunResult) -> None:
    adapter = FakeVenueAdapter(fill=True)
    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    events = await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        envelope=_env(min_edge_bps=0),
        adapter=adapter,
        entries_by_agent=_eligible(rr),
        execution_mode="live_guarded",
        base_seq=0,
        event_ts=0,
    )
    submitted = [e for e in events if e.event_type.value == "execution_submitted"]
    assert submitted and adapter.submit_calls == len(submitted)
    recs = await store.list_executions("c")
    assert any(r.status.value == "filled" and r.receipt is not None for r in recs)


async def test_paper_mode_law_approved_no_submit(rr: RunResult) -> None:
    """Paper mode NEVER submits: clean post-quote approvals park at ``law_approved`` (no fill).

    With the two-phase gate (Pre-2C Task 11) the post-quote pass is now LIVE, so a proposal whose
    forward executable edge has decayed at the quoted price is correctly ``rejected`` even in paper
    mode (the inert-gate fix). The invariant under test is the paper-mode one: no order is ever
    submitted, every event is a ``policy_result``, and at least one clean proposal parks at
    ``law_approved`` (never ``submitted``/``filled``).
    """
    adapter = FakeVenueAdapter()
    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    events = await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        envelope=_env(min_edge_bps=0),
        adapter=adapter,
        entries_by_agent=_eligible(rr),
        execution_mode="paper",
        base_seq=0,
        event_ts=0,
    )
    assert all(e.event_type.value == "policy_result" for e in events)
    assert adapter.submit_calls == 0
    recs = await store.list_executions("c")
    # Paper mode never reaches a fill terminal; clean approvals stay law_approved, decayed-edge
    # takes are rejected by the (now-live) post-quote gate — but nothing is ever submitted/filled.
    assert recs and all(r.status.value in ("law_approved", "rejected") for r in recs)
    assert any(r.status.value == "law_approved" for r in recs)


async def test_kill_switch_denies_collects_reasons_no_submit(rr: RunResult) -> None:
    adapter = FakeVenueAdapter()
    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    events = await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        envelope=_env(kill_switch=True),
        adapter=adapter,
        entries_by_agent=_eligible(rr),
        execution_mode="live_guarded",
        base_seq=0,
        event_ts=0,
    )
    policy_events = [e for e in events if e.event_type.value == "policy_result"]
    assert policy_events
    for ev in policy_events:
        assert ev.payload["decision"] == "denied"
        assert "kill_switch_on" in ev.payload["reason_codes"]
    assert adapter.submit_calls == 0
    recs = await store.list_executions("c")
    assert recs and all(r.status.value == "rejected" for r in recs)


async def test_events_are_seq_ordered_from_base_seq(rr: RunResult) -> None:
    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    events = await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        envelope=_env(min_edge_bps=0),
        adapter=FakeVenueAdapter(fill=True),
        entries_by_agent=_eligible(rr),
        execution_mode="dry_run",
        base_seq=500,
        event_ts=0,
    )
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)
    assert seqs == list(range(500, 500 + len(events)))  # contiguous from base_seq


async def test_idempotent_execution_ids_on_rerun(rr: RunResult) -> None:
    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    kwargs: dict[str, object] = {
        "competition_id": "c",
        "run_result": rr,
        "envelope": _env(min_edge_bps=0),
        "adapter": FakeVenueAdapter(fill=True),
        "entries_by_agent": _eligible(rr),
        "execution_mode": "dry_run",
        "base_seq": 0,
        "event_ts": 0,
    }
    await run_execution_lane(store, **kwargs)  # type: ignore[arg-type]
    first = [r.execution_id for r in await store.list_executions("c")]
    await run_execution_lane(store, **kwargs)  # type: ignore[arg-type]
    second = [r.execution_id for r in await store.list_executions("c")]
    assert first == second  # stable ids -> upsert, no duplicates


# ---------------------------------------------------------------------------
# Stake sizing — sealed half-Kelly, capped (reads ONLY law output)
# ---------------------------------------------------------------------------


def test_size_stake_half_kelly_against_bankroll() -> None:
    # 0.5 * kelly * bankroll, below the cap.
    assert _size_stake(0.1, bankroll=1000.0, max_stake=1000.0) == 50.0


def test_size_stake_capped_at_max_stake() -> None:
    # 0.5 * 1.0 * 1000 = 500, capped at the policy max_stake.
    assert _size_stake(1.0, bankroll=1000.0, max_stake=100.0) == 100.0


def test_size_stake_nonpositive_kelly_falls_back() -> None:
    assert _size_stake(0.0, bankroll=1000.0, max_stake=1000.0) == _FALLBACK_STAKE
    assert _size_stake(-0.5, bankroll=1000.0, max_stake=1000.0) == _FALLBACK_STAKE
    # The fallback is itself capped at max_stake.
    assert _size_stake(0.0, bankroll=1000.0, max_stake=0.5) == 0.5


def test_size_stake_nan_falls_back() -> None:
    # NaN <= 0.0 is False, so an undefined kelly must be caught by the explicit isnan guard
    # (otherwise min(NaN, max_stake) -> NaN order size).
    assert _size_stake(float("nan"), bankroll=1000.0, max_stake=100.0) == min(_FALLBACK_STAKE, 100.0)


async def test_submitted_event_size_reflects_sealed_kelly_sizing(rr: RunResult) -> None:
    """The submitted order size is the SEALED-Kelly-sized stake (fixture kelly≈0 -> fallback)."""
    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    events = await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        envelope=_env(min_edge_bps=0),
        adapter=FakeVenueAdapter(fill=True),
        entries_by_agent=_eligible(rr),
        execution_mode="dry_run",
        base_seq=0,
        event_ts=0,
        bankroll=1000.0,
    )
    submitted = [e for e in events if e.event_type.value == "execution_submitted"]
    assert submitted  # fixture markets are near-fair -> sealed kelly is 0 -> fallback stake
    assert all(e.payload["size"] == _FALLBACK_STAKE for e in submitted)
    # The receipt mirrors the requested size.
    recs = await store.list_executions("c")
    filled = [r for r in recs if r.receipt is not None]
    assert filled
    for record in filled:
        assert record.receipt is not None and record.receipt.requested_size == _FALLBACK_STAKE


async def test_positive_kelly_sizes_order_end_to_end(rr: RunResult) -> None:
    """A positive sealed kelly_fraction sizes the order end-to-end (NOT the fallback path)."""
    # Give a SELECTED (valid, edge>=0, taker) row a positive sealed Kelly fraction.
    target = next(
        row
        for row in rr.score_rows
        if row["valid"] is True
        and row["recomputed_edge_bps"] >= 0
        and row.get("raw_prescore", {}).get("raw_action", {}).get("params", {}).get("market_key")
    )
    target["kelly_fraction"] = 0.1

    # Correlate the row to its execution_id the same way the lane does (via the strategy).
    prop = next(
        p
        for p in value_proposals(rr, min_edge_bps=0)
        if (p.agent_id, p.tick_seq) == (target["agent_id"], target["tick_seq"])
    )
    execution_id = f"{rr.run_id}:{prop.source_sequence_no}"

    env = _env(min_edge_bps=0)  # max_stake = 100.0
    expected = min(0.5 * 0.1 * 1000.0, env.max_stake)  # 50.0 — half-Kelly, below the cap
    assert expected != _FALLBACK_STAKE  # this is the NON-fallback Kelly path

    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    events = await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        envelope=env,
        adapter=FakeVenueAdapter(fill=True),
        entries_by_agent=_eligible(rr),
        execution_mode="dry_run",
        base_seq=0,
        event_ts=0,
        bankroll=1000.0,
    )

    submitted = next(
        e for e in events if e.event_type.value == "execution_submitted" and e.payload["execution_id"] == execution_id
    )
    assert submitted.payload["size"] == expected
    record = await store.get_execution_record(execution_id)
    assert record.receipt is not None and record.receipt.requested_size == expected


# ---------------------------------------------------------------------------
# Two-phase gate (Pre-2C Task 11) — pre-quote denies BEFORE I/O; post-quote uses
# the REAL slippage + forward executable-edge from the actual venue quote.
# ---------------------------------------------------------------------------


class _RecordingAdapter(FakeVenueAdapter):
    """FakeVenueAdapter that counts quote calls and returns a chosen price (for gate tests)."""

    # Explicit venue slug so the policy allowlist (which lists "fake") matches — otherwise the
    # class-name-derived slug ("_recording") would trip ``venue_not_allowed`` at pre-quote and the
    # post-quote path could never be reached.
    venue = "fake"

    def __init__(self, *, price: float, fill: bool = True) -> None:
        super().__init__(fill=fill)
        self._price = price
        self.quote_calls = 0

    async def quote_market(self, market_ref: str) -> Quote:
        self.quote_calls += 1
        return Quote(market_ref=market_ref, price=self._price, size=500.0, ts=int(time.time()))


async def test_pre_quote_kill_switch_skips_venue_quote(rr: RunResult) -> None:
    """Pre-quote kill-switch deny: NO venue quote, NO submit, POLICY_RESULT.phase == 'pre_quote'."""
    adapter = _RecordingAdapter(price=2.05)
    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    events = await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        envelope=_env(kill_switch=True),
        adapter=adapter,
        entries_by_agent=_eligible(rr),
        execution_mode="dry_run",
        base_seq=1000,
        event_ts=0,
    )
    assert adapter.quote_calls == 0  # pre-quote gate denied BEFORE any venue I/O
    assert adapter.submit_calls == 0
    policy = [e for e in events if e.event_type == EventType.POLICY_RESULT]
    assert policy and all(e.payload["phase"] == "pre_quote" for e in policy)
    assert all("kill_switch_on" in e.payload["reason_codes"] for e in policy)
    assert not any(e.event_type == EventType.EXECUTION_SUBMITTED for e in events)


async def test_post_quote_slippage_denies_submit(rr: RunResult) -> None:
    """Quote far from the sealed reference price trips slippage_over_max → quoted once, no submit."""
    adapter = _RecordingAdapter(price=9.0)  # far from the sealed entry price (~1.88–2.14)
    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    events = await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        # min_edge negative so BOTH proposals clear the pre-quote edge screen and reach the quote;
        # tiny slippage cap so the post-quote gate trips on the 9.0 quote.
        envelope=_env(min_edge_bps=-100000, max_slippage_bps=50, max_price=1.0e9),
        adapter=adapter,
        entries_by_agent=_eligible(rr),
        execution_mode="dry_run",
        base_seq=1000,
        event_ts=0,
    )
    assert adapter.quote_calls >= 1  # pre-quote passed → venue WAS quoted
    assert adapter.submit_calls == 0  # post-quote slippage tripped → NO submit
    post = [e for e in events if e.event_type == EventType.POLICY_RESULT and e.payload.get("phase") == "post_quote"]
    assert post
    assert any("slippage_over_max" in e.payload["reason_codes"] for e in post)
    # the post-quote payload surfaces the real, price-dependent numbers (the inert-gate fix):
    assert all("slippage_bps" in e.payload and "executable_edge_bps" in e.payload for e in post)
    assert not any(e.event_type == EventType.EXECUTION_SUBMITTED for e in events)


async def test_receipt_independence_unchanged(rr: RunResult) -> None:
    """The existing SEC-004 invariant still holds after the gate rewire: receipts never touch the seal."""
    board_before = leaderboard([dict(r) for r in score_run(rr)])
    ev_before = rr.evidence_hash
    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        envelope=_env(min_edge_bps=0),
        adapter=FakeVenueAdapter(fill=True),
        entries_by_agent=_eligible(rr),
        execution_mode="dry_run",
        base_seq=1000,
        event_ts=0,
    )
    assert rr.evidence_hash == ev_before
    assert leaderboard([dict(r) for r in score_run(rr)]) == board_before
