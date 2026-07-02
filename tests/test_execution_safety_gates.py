"""T20b-1 — live_guarded execution-safety gates (runner), TDD.

The runner is the HIGHEST-STAKES surface: on the live-guarded path it moves real money on a real
venue. Every gate here is FAIL-CLOSED — when anything is uncertain the live submit does NOT arm.
These tests are fully offline (FakeVenueAdapter / marker adapters + an injected clock); no real
venue, no real money. DRY_RUN is the default; a real live_guarded submit is an operator-only step.

Four gates:

1. QUOTE-SIZE COUPLING — the venue quote is priced for the SAME size the order submits, so the
   slippage / executable_edge the policy gates on match the size that fills.
2. (preflight live_ready lives in ``test_polymarket_preflight.py``.)
3. CIRCUIT BREAKER + LIVE CAP — an OPEN breaker blocks the live submit; only EXECUTED outcomes
   move it (policy-denials never do); the tighter live-guarded stake cap applies on the live path.
4. real_venue_quote EARNED — the POLICY_RESULT flag is true ONLY from a genuine real-venue
   adapter, NEVER inferred from the presence of a venue price / edge, NEVER from a Fake adapter.
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
from veridex.execution.runner import BreakerCell, run_execution_lane
from veridex.policy.circuit_breaker import CircuitBreaker, CircuitState
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.orchestrator import RunResult
from veridex.store import InMemoryStore
from veridex.venues.base import Quote
from veridex.venues.sx_bet import FakeVenueAdapter

_VENUE = "fake"


def _env(**overrides: object) -> PolicyEnvelope:
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
        "human_approval_threshold": 1.0e12,
        "kill_switch": False,
    }
    base.update(overrides)
    return PolicyEnvelope(**base)  # type: ignore[arg-type]


@pytest.fixture
def rr() -> RunResult:
    return finished_run_result()


def _eligible(rr: RunResult) -> dict[str, AgentEntry]:
    return {
        agent_id: AgentEntry(
            agent_id=agent_id,
            owner="u",
            strategy="value",
            model=None,
            proof_mode="reproducible",
            execution_eligibility=True,
        )
        for agent_id in rr.agent_ids
    }


class _SizeRecordingAdapter(FakeVenueAdapter):
    """FakeVenueAdapter that records the ``for_size`` each quote was priced for (gate-1 coupling).

    Explicit ``venue = "fake"`` so the class-name-derived slug does not trip ``venue_not_allowed``.
    Carries NO real-venue marker, so it must always yield ``real_venue_quote=False`` (gate 4).
    """

    venue = "fake"

    def __init__(self, *, price: float = 2.05, fill: bool = True, fill_size: float | None = None) -> None:
        super().__init__(fill=fill, fill_size=fill_size)
        self._price = price
        self.for_sizes: list[float | None] = []
        self.quote_calls = 0

    async def quote_market(self, market_ref: str, for_size: float | None = None) -> Quote:
        self.quote_calls += 1
        self.for_sizes.append(for_size)
        return Quote(market_ref=market_ref, price=self._price, size=500.0, for_size=for_size, ts=int(time.time()))


class _RealMarkerAdapter(_SizeRecordingAdapter):
    """A genuine-venue stand-in: declares the real-venue-quote marker the runner keys on."""

    PROVIDES_REAL_VENUE_QUOTE = True


async def _lane(rr: RunResult, adapter: FakeVenueAdapter, *, mode: str, envelope: PolicyEnvelope, **kw: object) -> list:
    store = InMemoryStore()
    comp = Competition(
        competition_id="c",
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
    await store.create_competition(comp)
    events = await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        envelope=envelope,
        adapter=adapter,
        entries_by_agent=_eligible(rr),
        execution_mode=mode,
        base_seq=0,
        event_ts=0,
        **kw,  # type: ignore[arg-type]
    )
    return events


def _post_quote(events: list) -> list:
    return [e for e in events if e.event_type == EventType.POLICY_RESULT and e.payload.get("phase") == "post_quote"]


def _pre_quote(events: list) -> list:
    return [e for e in events if e.event_type == EventType.POLICY_RESULT and e.payload.get("phase") == "pre_quote"]


# ---------------------------------------------------------------------------
# Gate 1 — quote-size coupling
# ---------------------------------------------------------------------------


async def test_quote_priced_for_the_size_that_submits(rr: RunResult) -> None:
    """The quote is requested ``for_size=stake`` — the SAME size the order submits (no mismatch)."""
    adapter = _SizeRecordingAdapter(fill=True)
    events = await _lane(rr, adapter, mode="dry_run", envelope=_env(min_edge_bps=0))

    submitted = [e for e in events if e.event_type == EventType.EXECUTION_SUBMITTED]
    assert submitted  # at least one order placed
    assert adapter.for_sizes and all(fs is not None for fs in adapter.for_sizes)  # never a default None
    # Every quote was priced for exactly the size that submitted (coupling): the set of order
    # sizes equals the set of for_sizes the venue was quoted for.
    submitted_sizes = sorted(e.payload["size"] for e in submitted)
    priced_sizes = sorted(fs for fs in adapter.for_sizes if fs is not None)
    # every submitted size appears as a for_size the quote was priced for
    for size in submitted_sizes:
        assert size in priced_sizes


# ---------------------------------------------------------------------------
# Gate 3 — circuit breaker + live cap
# ---------------------------------------------------------------------------


async def test_open_breaker_blocks_live_submit(rr: RunResult) -> None:
    """An OPEN breaker denies at the PRE-quote gate — no venue quote, no live submit (fail-closed)."""
    cell = BreakerCell(
        CircuitBreaker(state=CircuitState.OPEN, consecutive_failures=5, opened_at=0.0),
        cooldown_s=1.0e9,  # cooldown not elapsed → resolve keeps it OPEN
    )
    adapter = _SizeRecordingAdapter(fill=True)
    events = await _lane(
        rr, adapter, mode="live_guarded", envelope=_env(min_edge_bps=0), guards=cell, now=0.0
    )

    assert adapter.quote_calls == 0  # OPEN denied BEFORE any venue I/O
    assert adapter.submit_calls == 0  # never armed the live submit
    pre = _pre_quote(events)
    assert pre and all("circuit_open" in e.payload["reason_codes"] for e in pre)
    assert cell.breaker.state is CircuitState.OPEN  # stayed OPEN (no probe admitted)


async def test_executed_failure_trips_breaker(rr: RunResult) -> None:
    """A real EXECUTED failure (rejected receipt) trips the breaker; the next proposal is blocked."""
    cell = BreakerCell(CircuitBreaker(), cooldown_s=0.0)  # CLOSED
    adapter = _SizeRecordingAdapter(fill=False)  # submit → rejected receipt (an executed failure)
    events = await _lane(
        rr,
        adapter,
        mode="live_guarded",
        envelope=_env(min_edge_bps=0, circuit_breaker_threshold=1),
        guards=cell,
        now=0.0,
    )

    assert cell.breaker.state is CircuitState.OPEN  # the executed failure opened it
    assert adapter.submit_calls == 1  # first proposal executed+failed; the breaker blocked the rest
    # the later proposal was denied by the now-OPEN breaker (fail-closed, no further submit)
    assert any("circuit_open" in e.payload["reason_codes"] for e in _pre_quote(events))


async def test_policy_denial_does_not_move_breaker(rr: RunResult) -> None:
    """A policy-denied (never-executed) outcome does NOT move the breaker — only real fills/failures do."""
    cell = BreakerCell(CircuitBreaker(), cooldown_s=0.0)
    adapter = _SizeRecordingAdapter(fill=True)
    await _lane(
        rr,
        adapter,
        mode="live_guarded",
        envelope=_env(kill_switch=True, circuit_breaker_threshold=1),  # kill-switch denies everything
        guards=cell,
        now=0.0,
    )

    assert adapter.submit_calls == 0  # nothing executed
    assert cell.breaker.state is CircuitState.CLOSED  # a denial is not an executed failure
    assert cell.breaker.consecutive_failures == 0


async def test_live_cap_blocks_live_submit(rr: RunResult) -> None:
    """The tighter live-guarded stake cap denies the live submit when the stake exceeds it."""
    adapter = _SizeRecordingAdapter(fill=True)
    # fixture kelly≈0 → fallback stake 1.0; a live cap of 0.5 (< 1.0) trips ONLY on the live path.
    events = await _lane(
        rr, adapter, mode="live_guarded", envelope=_env(min_edge_bps=0, max_stake_live_guarded=0.5)
    )

    assert adapter.submit_calls == 0
    pre = _pre_quote(events)
    assert pre and all("stake_over_live_guarded" in e.payload["reason_codes"] for e in pre)


async def test_live_cap_does_not_apply_on_dry_run(rr: RunResult) -> None:
    """The live-guarded cap is inert on dry_run — only the live-money path is affected (fail-closed scope)."""
    adapter = _SizeRecordingAdapter(fill=True)
    events = await _lane(
        rr, adapter, mode="dry_run", envelope=_env(min_edge_bps=0, max_stake_live_guarded=0.5)
    )
    # dry_run simulates a fill; the live cap never fires (no stake_over_live_guarded).
    assert any(e.event_type == EventType.EXECUTION_SUBMITTED for e in events)
    assert not any("stake_over_live_guarded" in e.payload.get("reason_codes", []) for e in _pre_quote(events))


# ---------------------------------------------------------------------------
# Gate 4 — real_venue_quote earned
# ---------------------------------------------------------------------------


async def test_real_venue_quote_true_only_from_real_adapter(rr: RunResult) -> None:
    """A genuine real-venue adapter earns ``real_venue_quote=true`` on the POLICY_RESULT."""
    adapter = _RealMarkerAdapter(price=2.05)
    events = await _lane(rr, adapter, mode="dry_run", envelope=_env(min_edge_bps=0))
    post = _post_quote(events)
    assert post and all(e.payload["real_venue_quote"] is True for e in post)


async def test_real_venue_quote_false_for_fake_adapter_even_with_venue_price(rr: RunResult) -> None:
    """A Fake adapter NEVER earns it — even though its quote carries a venue price / edge (never inferred)."""
    adapter = _SizeRecordingAdapter(price=2.05)  # a Fake: carries venue_decimal_price + executable edge
    events = await _lane(rr, adapter, mode="dry_run", envelope=_env(min_edge_bps=0))
    post = _post_quote(events)
    assert post
    for e in post:
        assert e.payload["real_venue_quote"] is False  # fail-closed default
        # the flag is NEVER inferred from the presence of these price-dependent numbers:
        assert e.payload.get("venue_decimal_price") is not None
        assert "executable_edge_bps" in e.payload


def test_real_venue_adapters_declare_the_marker_fakes_do_not() -> None:
    """Only a genuine venue adapter declares the marker; Fake / SX-skeleton do not (fail-closed)."""
    from veridex.venues.polymarket import PolymarketAdapter
    from veridex.venues.sx_bet import SXBetAdapter

    assert getattr(PolymarketAdapter, "PROVIDES_REAL_VENUE_QUOTE", False) is True
    assert getattr(FakeVenueAdapter, "PROVIDES_REAL_VENUE_QUOTE", False) is False
    assert getattr(SXBetAdapter, "PROVIDES_REAL_VENUE_QUOTE", False) is False
