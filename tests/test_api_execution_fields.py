"""REQ-2D-501 — flagship edge-legibility fields on the execution surface (TDD).

Pins the THREE edge-legibility formulas EXACTLY and asserts the executor lane's post-quote
``POLICY_RESULT`` payload surfaces ``mispricing_gap_bps`` + ``venue_decimal_price`` +
``native_price`` ALONGSIDE the law's ``executable_edge_bps`` — all four derived from the REAL
venue quote:

    venue_implied_prob_bps = round(10000 / venue_decimal_price)
    mispricing_gap_bps     = fair_prob_bps - venue_implied_prob_bps
    executable_edge_bps    = round((fair_prob * venue_decimal_price - 1) * 10000)   # law/edge.py

``mispricing_gap`` is a PROBABILITY-space dislocation (explanatory only, never scored — spec §2 /
CON-2D-501); ``executable_edge`` is the EV-space law quantity that gates execution. They are
distinct axes and never interchangeable. ``veridex.law.edge`` is UNCHANGED — cross-checked here.
"""

from __future__ import annotations

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
from veridex.execution.legibility import mispricing_gap_bps, venue_implied_prob_bps
from veridex.execution.runner import run_execution_lane
from veridex.law.edge import executable_edge_bps
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.orchestrator import RunResult
from veridex.store import InMemoryStore
from veridex.strategies.value import value_proposals
from veridex.venues.sx_bet import FakeVenueAdapter

# --------------------------------------------------------------------------------------
# Pure formula pins — the three legibility formulas, computed EXACTLY.
# --------------------------------------------------------------------------------------


def test_venue_implied_prob_bps_pinned() -> None:
    """``round(10000 / venue_decimal_price)`` — the book's implied probability in bps."""
    assert venue_implied_prob_bps(2.00) == 5000
    assert venue_implied_prob_bps(2.50) == 4000
    assert venue_implied_prob_bps(3.00) == 3333  # round(3333.33...)
    assert venue_implied_prob_bps(1.472) == 6793  # round(6793.478...)


def test_venue_implied_prob_bps_non_positive_is_zero() -> None:
    """No real quote (non-positive price) ⇒ no implied probability — fail-safe, never raises."""
    assert venue_implied_prob_bps(0.0) == 0
    assert venue_implied_prob_bps(-1.0) == 0


def test_mispricing_gap_bps_pinned() -> None:
    """``fair_prob_bps - venue_implied_prob_bps`` — prob-space dislocation, EXACT."""
    # fair 5500 bps, price 2.00 → implied 5000 → gap +500
    assert mispricing_gap_bps(5500, 2.00) == 500
    # at the fair price the gap is exactly 0 (de-margined doctrine — never re-de-vig):
    # fair 4000, price 2.5 → implied 4000 → gap 0
    assert mispricing_gap_bps(4000, 2.5) == 0
    # fair 4000, price 3.0 → implied round(3333.33)=3333 → gap +667
    assert mispricing_gap_bps(4000, 3.0) == 667


def test_executable_edge_law_unchanged_pin() -> None:
    """LAW form ``round((fair_prob*price - 1)*10000)`` — UNCHANGED; the same pins the gap shares."""
    assert executable_edge_bps(5500, 2.00) == 1000  # 0.55*2 - 1 = 0.10
    assert executable_edge_bps(4000, 2.5) == 0  # priced at fair → no edge
    assert executable_edge_bps(4000, 3.0) == 2000  # 0.40*3 - 1 = 0.20


def test_gap_and_edge_are_distinct_quantities() -> None:
    """mispricing_gap (prob-space) ≠ executable_edge (EV-space): different axes, never equal here."""
    fair_bps, price = 4000, 3.0
    assert mispricing_gap_bps(fair_bps, price) == 667  # probability space
    assert executable_edge_bps(fair_bps, price) == 2000  # expected-value space
    assert mispricing_gap_bps(fair_bps, price) != executable_edge_bps(fair_bps, price)


# --------------------------------------------------------------------------------------
# Execution-surface exposure — the post-quote POLICY_RESULT payload carries all four fields.
# --------------------------------------------------------------------------------------

_VENUE = "fake"


def _env(**overrides: object) -> PolicyEnvelope:
    base: dict[str, object] = {
        "max_stake": 100.0,
        "max_orders_per_run": 100,
        "max_orders_per_session": 100,
        "max_orders_per_day": 100,
        "venue_allowlist": [_VENUE],
        "market_allowlist": [_DEMO_MARKET_KEY],
        "min_edge_bps": -100000,  # both proposals clear the pre-quote edge screen and reach the quote
        "max_slippage_bps": 10_000,
        "max_price": 1.0e9,
        "max_quote_age_s": 10**9,
        "cooldown_s": 0,
        "human_approval_threshold": 1.0e12,
        "kill_switch": False,
    }
    base.update(overrides)
    return PolicyEnvelope(**base)  # type: ignore[arg-type]


def _comp(competition_id: str) -> Competition:
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


@pytest.fixture
def rr() -> RunResult:
    """Build the sealed fixture run in SYNC context (the fixture uses ``asyncio.run`` internally),
    resolved before the async test body enters the event loop."""
    return finished_run_result()


async def test_post_quote_payload_exposes_all_four_legibility_fields(rr: RunResult) -> None:
    """The post-quote POLICY_RESULT payload carries mispricing_gap_bps + venue_decimal_price +
    native_price ALONGSIDE executable_edge_bps — each from the REAL venue quote, computed EXACTLY."""
    adapter = FakeVenueAdapter()
    store = InMemoryStore()
    await store.create_competition(_comp("c"))
    events = await run_execution_lane(
        store,
        competition_id="c",
        run_result=rr,
        envelope=_env(),
        adapter=adapter,
        entries_by_agent=_eligible(rr),
        execution_mode="dry_run",
        base_seq=1000,
        event_ts=0,
    )

    post = [
        e
        for e in events
        if e.event_type == EventType.POLICY_RESULT and e.payload.get("phase") == "post_quote"
    ]
    assert post, "expected at least one post-quote POLICY_RESULT"

    # A reference quote pins the exact price/native the payload must echo from the real quote.
    ref_quote = await adapter.quote_market(_DEMO_MARKET_KEY)
    props = {
        f"{rr.run_id}:{p.source_sequence_no}": p
        for p in value_proposals(rr, min_edge_bps=-100000)
    }

    for e in post:
        pl = e.payload
        assert {
            "executable_edge_bps",
            "mispricing_gap_bps",
            "venue_decimal_price",
            "native_price",
        } <= set(pl.keys())
        # venue price + native ride straight off the real quote (audit-honest).
        assert pl["venue_decimal_price"] == ref_quote.price
        assert pl["native_price"] == ref_quote.native_price
        # gap + edge are recomputed EXACTLY from the sealed fair probability + the real price.
        prop = props[pl["execution_id"]]
        assert pl["mispricing_gap_bps"] == mispricing_gap_bps(prop.entry_prob_bps, ref_quote.price)
        assert pl["executable_edge_bps"] == executable_edge_bps(prop.entry_prob_bps, ref_quote.price)
