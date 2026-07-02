"""T20b-3 — operator LIVE-PATH wiring through the competition service (TDD).

T20b-1 built the fail-closed execution gates in the runner/preflight (BreakerCell, the live
stake cap, ``real_venue_quote``, quote-size coupling) and T20b-2 the fail-closed resolver, but
``service.py`` still routed the live_guarded path to the ``SXBetAdapter`` SKELETON and never
threaded the guards / ``live_ready`` into the runner — so the gates existed but the operator
live path did not USE them. These tests pin the closing seam:

  * ``_select_execution_route`` picks the adapter + effective mode + breaker guards FAIL-CLOSED:
    dry_run → the deterministic Fake (no guards); live_guarded ARMS the operator's real adapter
    ONLY when ``live_ready`` is True AND the adapter is a GENUINE real-venue adapter, otherwise it
    degrades to a dry-run simulation (no real order path is ever reached).
  * end-to-end through ``start_competition`` (with the operator ``live_deps`` bundle): an armed
    run routes the real adapter + a ``BreakerCell`` + the live-guarded mode into the runner
    (``real_venue_quote`` True, the live stake cap engages); ``live_ready=False`` arms NOTHING
    (the real adapter is never called); and an OPEN breaker blocks the rest of the run.

FULLY OFFLINE: in-memory store, deterministic fixture ticks + agents, a mock real-venue adapter
whose ``submit`` is captured in-process — NO network, NO real venue, NO real money. Real
live_guarded is OPERATOR-ONLY (write_enabled AND not dry_run AND live_ready AND a real adapter)
and is never exercised here.
"""

from __future__ import annotations

import pytest

from tests._arena_fixtures import _beta_agent, _ticks
from veridex.competition.events import EventType
from veridex.competition.models import (
    AgentEntry,
    CompetitionConfig,
    CompetitionStatus,
    CompetitionType,
    ExecutionMode,
)
from veridex.competition.service import (
    _DEGRADE_LIVE_READY_FALSE,
    _DEGRADE_MISSING_LIVE_DEPS,
    _DEGRADE_NON_REAL_ADAPTER,
    LiveExecutionDeps,
    _live_arm_gate,
    _select_execution_route,
    create_competition,
    register_agent,
    start_competition,
)
from veridex.execution.runner import BreakerCell
from veridex.policy.circuit_breaker import CircuitState
from veridex.policy.engine import _REASON_CIRCUIT_OPEN, _REASON_STAKE_OVER_LIVE_GUARDED
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.orchestrator import deterministic_agent
from veridex.store import InMemoryStore
from veridex.venues.base import Order, OrderStatus, Quote, SubmitAck, build_receipt
from veridex.venues.sx_bet import FakeVenueAdapter

# The one market key the deterministic fixture proposals all target (see the T20b-3 probe).
_FIXTURE_MARKET = "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1"


# ---------------------------------------------------------------------------
# A mock GENUINE real-venue adapter — sets the real-venue-quote marker, captures submits.
# ---------------------------------------------------------------------------


class _MockRealVenueAdapter:
    """Stand-in for an ARMED PolymarketAdapter: real-venue marker + in-process submit capture.

    Declares :data:`PROVIDES_REAL_VENUE_QUOTE` (so the lane earns ``real_venue_quote=True`` and
    ``_select_execution_route`` treats it as a genuine real adapter) but performs NO network I/O —
    ``submit_order`` merely records the call. ``fill_status`` drives the polled terminal status so a
    test can force a FILLED (success) or a REJECTED (failure → trips the breaker) outcome.
    """

    PROVIDES_REAL_VENUE_QUOTE: bool = True

    def __init__(self, *, price: float = 1.90, size: float = 1000.0, fill_status: str = "filled") -> None:
        self.venue = "polymarket"
        self._price = price
        self._size = size
        self._fill_status = fill_status
        self.submit_calls = 0
        self.quote_calls = 0

    async def quote_market(self, market_ref: str, for_size: float | None = None) -> Quote:
        self.quote_calls += 1
        return Quote(
            market_ref=market_ref,
            price=self._price,
            native_price=1.0 / self._price,
            size=self._size,
            for_size=for_size,
            levels=[],
            ts=10**11,  # far-future seconds so quote_age math never trips a stale gate here
        )

    async def submit_order(self, order: Order) -> SubmitAck:
        self.submit_calls += 1
        return SubmitAck(venue_order_id=f"live-{order.client_order_id}", accepted=True)

    async def get_order_status(self, venue_order_id: str) -> OrderStatus:
        filled = 1.0 if self._fill_status in ("filled", "partial") else 0.0
        return OrderStatus(
            venue_order_id=venue_order_id,
            status=self._fill_status,
            filled_size=filled,
            price=self._price,
            native_price=1.0 / self._price,
        )

    def normalize_receipt(self, execution_id: str, order: Order, status: OrderStatus, *, mode: str):  # noqa: ANN201
        return build_receipt(execution_id, order, status, mode=mode)


# ---------------------------------------------------------------------------
# Envelope + competition helpers
# ---------------------------------------------------------------------------


def _permissive_envelope(
    *,
    max_stake_live_guarded: float = 0.0,
    circuit_breaker_threshold: int = 0,
    cooldown_s: int = 0,
) -> PolicyEnvelope:
    """An envelope that ADMITS the fixture proposals to submit, so the live wiring is exercised.

    Wide price/slippage/quote-age/stake caps + a very high human-approval threshold (so a clean
    action AUTO-approves instead of escalating). The live-money knobs (``max_stake_live_guarded`` /
    ``circuit_breaker_threshold``) are parameterised so a test can force the live cap or the breaker.
    """
    return PolicyEnvelope(
        max_stake=1000.0,
        max_orders_per_run=10,
        max_orders_per_session=10,
        max_orders_per_day=10,
        # Allow both the armed real adapter ("polymarket") AND the degrade Fake ("fake") so a
        # fail-closed degrade still reaches the (dry-run) submit path and is observable in a test.
        venue_allowlist=["polymarket", "fake"],
        market_allowlist=[_FIXTURE_MARKET],
        min_edge_bps=0,
        max_slippage_bps=10**9,
        max_price=10**9,
        max_quote_age_s=10**12,
        cooldown_s=cooldown_s,
        human_approval_threshold=10**9,
        max_stake_live_guarded=max_stake_live_guarded,
        circuit_breaker_threshold=circuit_breaker_threshold,
        kill_switch=False,
    )


async def _seed_live_competition(
    store: InMemoryStore,
    *,
    envelope: PolicyEnvelope,
    execution_mode: ExecutionMode = ExecutionMode.LIVE_GUARDED,
) -> str:
    """Create a live/dry competition with the two fixture agents marked execution-eligible."""
    config = CompetitionConfig(
        competition_type=CompetitionType.LIVE_ARENA,
        source_mode="replay",
        execution_mode=execution_mode,
        market_scope="WC:TEST",
        roster_size=2,
        policy_envelope=envelope,
    )
    comp = await create_competition(store, config)
    for agent in (deterministic_agent("agent-alpha"), _beta_agent()):
        await register_agent(
            store,
            comp.competition_id,
            AgentEntry(
                agent_id=agent.agent_id,
                owner="u",
                strategy="value",
                model="anthropic/claude-sonnet-4",
                proof_mode=agent.proof_mode,
                execution_eligibility=True,  # cleared to execute (so the lane reaches submit)
            ),
        )
    return comp.competition_id


def _agents() -> list:  # noqa: ANN201
    return [deterministic_agent("agent-alpha"), _beta_agent()]


# ---------------------------------------------------------------------------
# 1. _select_execution_route — the pure fail-closed routing decision
# ---------------------------------------------------------------------------


def test_route_dry_run_uses_fake_no_guards() -> None:
    """dry_run routes the deterministic Fake, keeps the dry mode, and threads NO breaker."""
    adapter, mode, guards = _select_execution_route(
        ExecutionMode.DRY_RUN, _permissive_envelope(), None
    )
    assert isinstance(adapter, FakeVenueAdapter)
    assert mode == ExecutionMode.DRY_RUN.value
    assert guards is None


def test_route_live_without_deps_fails_closed_to_dry() -> None:
    """live_guarded with NO operator deps arms nothing — degrades to a dry-run simulation."""
    adapter, mode, guards = _select_execution_route(
        ExecutionMode.LIVE_GUARDED, _permissive_envelope(), None
    )
    assert isinstance(adapter, FakeVenueAdapter)
    assert mode == ExecutionMode.DRY_RUN.value  # NOT live_guarded — no real order path
    assert guards is None


def test_route_live_not_ready_fails_closed_to_dry() -> None:
    """A real adapter but ``live_ready=False`` must NOT arm — the real adapter is not routed."""
    real = _MockRealVenueAdapter()
    adapter, mode, guards = _select_execution_route(
        ExecutionMode.LIVE_GUARDED,
        _permissive_envelope(),
        LiveExecutionDeps(adapter=real, live_ready=False),
    )
    assert adapter is not real  # fell back to a fresh Fake
    assert isinstance(adapter, FakeVenueAdapter)
    assert mode == ExecutionMode.DRY_RUN.value
    assert guards is None


def test_route_live_ready_but_non_real_adapter_fails_closed() -> None:
    """``live_ready=True`` but a NON-real-marker adapter must NOT arm (fail-closed display honesty)."""
    fake = FakeVenueAdapter()  # no PROVIDES_REAL_VENUE_QUOTE marker
    adapter, mode, guards = _select_execution_route(
        ExecutionMode.LIVE_GUARDED,
        _permissive_envelope(),
        LiveExecutionDeps(adapter=fake, live_ready=True),
    )
    assert adapter is not fake
    assert mode == ExecutionMode.DRY_RUN.value
    assert guards is None


def test_route_armed_live_routes_real_adapter_and_breaker() -> None:
    """live_ready=True + a real adapter ARMS: the real adapter, live mode, and a CLOSED BreakerCell."""
    real = _MockRealVenueAdapter()
    envelope = _permissive_envelope(cooldown_s=7, circuit_breaker_threshold=3)
    adapter, mode, guards = _select_execution_route(
        ExecutionMode.LIVE_GUARDED,
        envelope,
        LiveExecutionDeps(adapter=real, live_ready=True),
    )
    assert adapter is real  # the operator's real adapter is routed as-is
    assert mode == ExecutionMode.LIVE_GUARDED.value
    assert isinstance(guards, BreakerCell)
    assert guards.cooldown_s == float(envelope.cooldown_s)
    assert guards.breaker.state is CircuitState.CLOSED  # seeded closed; the runner owns transitions


@pytest.mark.parametrize("unexpected_mode", [ExecutionMode.PAPER, "future_live_mode_v2"])
def test_non_live_guarded_mode_never_arms_even_with_full_armed_deps(unexpected_mode: object) -> None:
    """STRUCTURAL fail-closed: ONLY ``live_guarded`` mode can arm — any other mode degrades to dry.

    Even with FULL armed operator deps (``live_ready=True`` + a real-marker adapter), a mode that is
    NOT ``LIVE_GUARDED`` — a defensively-passed ``paper`` OR a future/unknown ExecutionMode value —
    routes the Fake, ``dry_run``, and NO guards. This proves the arm predicate is gated on the mode
    STRUCTURALLY (first conjunct), not by incidental enum arithmetic or a distant caller.
    """
    real = _MockRealVenueAdapter()
    adapter, mode, guards = _select_execution_route(
        unexpected_mode,  # type: ignore[arg-type]  # deliberately an unexpected/future mode value
        _permissive_envelope(),
        LiveExecutionDeps(adapter=real, live_ready=True),
    )
    assert adapter is not real  # the armed real adapter is NEVER routed off the live_guarded mode
    assert isinstance(adapter, FakeVenueAdapter)
    assert mode == ExecutionMode.DRY_RUN.value
    assert guards is None


# ---------------------------------------------------------------------------
# 2. end-to-end through start_competition (the real seam)
# ---------------------------------------------------------------------------


async def test_armed_live_path_routes_real_adapter_and_guards() -> None:
    """An armed live run submits through the REAL adapter with mode=live_guarded + real_venue_quote."""
    store = InMemoryStore()
    real = _MockRealVenueAdapter(fill_status="filled")
    cid = await _seed_live_competition(store, envelope=_permissive_envelope())
    await start_competition(
        store, cid, _ticks(), _agents(), live_deps=LiveExecutionDeps(adapter=real, live_ready=True)
    )

    # The real adapter's submit path was actually reached (no network — captured in-process).
    assert real.submit_calls >= 1
    events = await store.list_competition_events(cid, since_seq=-1)
    submitted = [e for e in events if e.event_type == EventType.EXECUTION_SUBMITTED]
    assert submitted and all(e.payload["mode"] == ExecutionMode.LIVE_GUARDED.value for e in submitted)
    # real_venue_quote is EARNED from the real-marker adapter on the post-quote POLICY_RESULT.
    post = [e for e in events if e.event_type == EventType.POLICY_RESULT and e.payload.get("phase") == "post_quote"]
    assert post and any(e.payload.get("real_venue_quote") is True for e in post)


async def test_live_stake_cap_engages_on_armed_path() -> None:
    """The tighter live stake cap (live_guarded-only) DENIES pre-quote — proving live mode threaded."""
    store = InMemoryStore()
    real = _MockRealVenueAdapter(fill_status="filled")
    # Fallback stake for the (kelly=0) fixture proposals is 1.0; a 0.5 live cap denies every one.
    cid = await _seed_live_competition(store, envelope=_permissive_envelope(max_stake_live_guarded=0.5))
    await start_competition(
        store, cid, _ticks(), _agents(), live_deps=LiveExecutionDeps(adapter=real, live_ready=True)
    )

    assert real.submit_calls == 0  # capped BEFORE any venue I/O
    events = await store.list_competition_events(cid, since_seq=-1)
    pre = [e for e in events if e.event_type == EventType.POLICY_RESULT and e.payload.get("phase") == "pre_quote"]
    assert pre and any(_REASON_STAKE_OVER_LIVE_GUARDED in e.payload["reason_codes"] for e in pre)
    assert not any(e.event_type == EventType.EXECUTION_SUBMITTED for e in events)


async def test_live_ready_false_arms_nothing_end_to_end() -> None:
    """live_ready=False through the service arms NOTHING — the real adapter is never called."""
    store = InMemoryStore()
    real = _MockRealVenueAdapter(fill_status="filled")
    cid = await _seed_live_competition(store, envelope=_permissive_envelope())
    comp = await start_competition(
        store, cid, _ticks(), _agents(), live_deps=LiveExecutionDeps(adapter=real, live_ready=False)
    )

    assert comp.status == CompetitionStatus.FINALIZED
    assert real.submit_calls == 0  # NO real order path reached
    events = await store.list_competition_events(cid, since_seq=-1)
    submitted = [e for e in events if e.event_type == EventType.EXECUTION_SUBMITTED]
    # Degraded to a dry-run simulation: any submit is labelled dry_run, never live_guarded.
    assert all(e.payload["mode"] == ExecutionMode.DRY_RUN.value for e in submitted)
    post = [e for e in events if e.event_type == EventType.POLICY_RESULT and e.payload.get("phase") == "post_quote"]
    assert post and all(e.payload.get("real_venue_quote") is False for e in post)


async def test_open_breaker_blocks_rest_of_live_run() -> None:
    """A live failure trips the breaker (threshold=1); the remaining proposals deny circuit_open."""
    store = InMemoryStore()
    real = _MockRealVenueAdapter(fill_status="rejected")  # every live fill FAILS
    cid = await _seed_live_competition(store, envelope=_permissive_envelope(circuit_breaker_threshold=1))
    await start_competition(
        store, cid, _ticks(), _agents(), live_deps=LiveExecutionDeps(adapter=real, live_ready=True)
    )

    # Only the FIRST proposal reached submit; the failure opened the breaker before the rest.
    assert real.submit_calls == 1
    events = await store.list_competition_events(cid, since_seq=-1)
    denied = [
        e
        for e in events
        if e.event_type == EventType.POLICY_RESULT and _REASON_CIRCUIT_OPEN in e.payload.get("reason_codes", [])
    ]
    assert denied  # the mid-run trip blocked the remaining proposals end-to-end (fail-closed)


async def test_paper_mode_ignores_live_deps() -> None:
    """A paper competition never runs the executor lane, so live_deps is inert (no submit)."""
    store = InMemoryStore()
    real = _MockRealVenueAdapter()
    cid = await _seed_live_competition(
        store, envelope=_permissive_envelope(), execution_mode=ExecutionMode.PAPER
    )
    comp = await start_competition(
        store, cid, _ticks(), _agents(), live_deps=LiveExecutionDeps(adapter=real, live_ready=True)
    )
    assert comp.status == CompetitionStatus.FINALIZED
    assert real.submit_calls == 0
    events = await store.list_competition_events(cid, since_seq=-1)
    assert not any(e.event_type == EventType.EXECUTION_SUBMITTED for e in events)


@pytest.mark.parametrize("mode", [ExecutionMode.DRY_RUN, ExecutionMode.LIVE_GUARDED])
async def test_no_live_deps_default_is_fail_closed(mode: ExecutionMode) -> None:
    """The default call (no live_deps) never arms a real order — dry_run works, live degrades."""
    store = InMemoryStore()
    cid = await _seed_live_competition(store, envelope=_permissive_envelope(), execution_mode=mode)
    comp = await start_competition(store, cid, _ticks(), _agents())  # no live_deps
    assert comp.status == CompetitionStatus.FINALIZED
    events = await store.list_competition_events(cid, since_seq=-1)
    submitted = [e for e in events if e.event_type == EventType.EXECUTION_SUBMITTED]
    # With no operator deps there is never a live_guarded submit (dry_run simulates; live degrades).
    assert all(e.payload["mode"] == ExecutionMode.DRY_RUN.value for e in submitted)


# ---------------------------------------------------------------------------
# 3. Honest-degrade reason (T23) — a configured live_guarded run that degrades to
#    dry MUST self-describe WHY, as evidence=false ops telemetry (not sealed evidence).
# ---------------------------------------------------------------------------


def test_live_arm_gate_names_each_failed_money_gate() -> None:
    """``_live_arm_gate`` is the SINGLE authority for the live money gates: ``None`` iff FULLY armed,
    else the SPECIFIC gate-failure reason (checked in fail-closed order)."""
    real = _MockRealVenueAdapter()
    fake = FakeVenueAdapter()  # no PROVIDES_REAL_VENUE_QUOTE marker
    assert _live_arm_gate(None) == _DEGRADE_MISSING_LIVE_DEPS
    assert _live_arm_gate(LiveExecutionDeps(adapter=real, live_ready=False)) == _DEGRADE_LIVE_READY_FALSE
    assert _live_arm_gate(LiveExecutionDeps(adapter=fake, live_ready=True)) == _DEGRADE_NON_REAL_ADAPTER
    # FULLY armed → no degrade reason (the run may arm the real path).
    assert _live_arm_gate(LiveExecutionDeps(adapter=real, live_ready=True)) is None


@pytest.mark.parametrize(
    "make_deps,expected_reason",
    [
        (lambda real: None, _DEGRADE_MISSING_LIVE_DEPS),
        (lambda real: LiveExecutionDeps(adapter=real, live_ready=False), _DEGRADE_LIVE_READY_FALSE),
        (lambda real: LiveExecutionDeps(adapter=FakeVenueAdapter(), live_ready=True), _DEGRADE_NON_REAL_ADAPTER),
    ],
)
async def test_degraded_live_run_records_reason_end_to_end(make_deps: object, expected_reason: str) -> None:
    """A CONFIGURED live_guarded run that FAILS a money gate degrades to dry (no real order) AND
    records an EXECUTION_ROUTE telemetry event naming the specific gate that failed — evidence=False
    ops telemetry an auditor sees, never sealed into the evidence hash."""
    store = InMemoryStore()
    real = _MockRealVenueAdapter(fill_status="filled")
    cid = await _seed_live_competition(store, envelope=_permissive_envelope())
    await start_competition(store, cid, _ticks(), _agents(), live_deps=make_deps(real))  # type: ignore[operator]

    # Fail-closed UNCHANGED: the operator's real adapter is NEVER reached (no real order path).
    assert real.submit_calls == 0
    events = await store.list_competition_events(cid, since_seq=-1)
    route = [e for e in events if e.event_type == EventType.EXECUTION_ROUTE]
    assert len(route) == 1, "exactly one degrade-telemetry event per degraded run"
    ev = route[0]
    assert ev.evidence is False  # ops/telemetry, NOT sealed evidence
    assert ev.payload["degraded_because_not_armed"] is True
    assert ev.payload["degrade_reason"] == expected_reason
    assert ev.payload["requested_execution_mode"] == ExecutionMode.LIVE_GUARDED.value
    assert ev.payload["effective_execution_mode"] == ExecutionMode.DRY_RUN.value
    # Any submit that still happened is the dry-run simulation, never a live_guarded order.
    submitted = [e for e in events if e.event_type == EventType.EXECUTION_SUBMITTED]
    assert all(e.payload["mode"] == ExecutionMode.DRY_RUN.value for e in submitted)


async def test_armed_live_run_records_no_degrade_event() -> None:
    """A FULLY-armed live_guarded run does NOT degrade, so it emits NO degrade-telemetry event."""
    store = InMemoryStore()
    real = _MockRealVenueAdapter(fill_status="filled")
    cid = await _seed_live_competition(store, envelope=_permissive_envelope())
    await start_competition(
        store, cid, _ticks(), _agents(), live_deps=LiveExecutionDeps(adapter=real, live_ready=True)
    )
    events = await store.list_competition_events(cid, since_seq=-1)
    assert not [e for e in events if e.event_type == EventType.EXECUTION_ROUTE]


async def test_dry_run_competition_records_no_degrade_event() -> None:
    """A configured DRY_RUN run is not a degrade (it asked for dry) — no degrade telemetry emitted."""
    store = InMemoryStore()
    cid = await _seed_live_competition(store, envelope=_permissive_envelope(), execution_mode=ExecutionMode.DRY_RUN)
    await start_competition(store, cid, _ticks(), _agents())
    events = await store.list_competition_events(cid, since_seq=-1)
    assert not [e for e in events if e.event_type == EventType.EXECUTION_ROUTE]
