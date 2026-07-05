"""D2 — pre_match backtest close planning: stop at kickoff + per-market CON-040 close.

PURE, offline planning over an ordered ``list[MarketState]`` (no network, no LLM, no ``CompetitionRun``).
This module answers ONE question for a ``pre_match`` backtest: given a fixture's replayed ticks — which
may be a FULL match (pre-kickoff ticks + in-running ticks) — which ticks are pre-kickoff DECISIONS, and
what is the authoritative CON-040 CLOSE they are scored against?

The window doctrine (``veridex.runtime.window`` / ``veridex.runtime.live_runner.run_live_window``) already
defines ``pre_match`` as: *end at kickoff, score against the CON-040 reconstructed close.* This mirrors
that contract on the REPLAY side, at the ``MarketState`` level:

  * **Kickoff cutoff** — the FIRST ``phase == 1`` (in-running) tick. Decisions are every tick STRICTLY
    before it (pre-kickoff only). A later ``0`` after the first ``1`` (halftime re-quote) is ignored:
    the FIRST kickoff wins. Mirrors ``live_runner``'s "first IN-RUNNING tick TERMINATES the window".
  * **Per-market close (completeness contract)** — for each market scored in the decision stream, the
    close is the LAST pre-kickoff ``phase == 0`` line for THAT market, folded into ONE snapshot that
    carries EVERY scored market. This mirrors ``live_runner._reconstruct_closing_state``: a close that
    covers only one market makes every OTHER scored market fall back (via
    ``orchestrator._closing_snapshots``) to its last-seen decision tick and silently score CLV 0.
  * **Honest degrades (never fabricate a kickoff/close):**
      - never in-running (no ``phase == 1`` at all) → ALLOWED as a pre-match-only pack (still true CLV),
        but the plan carries a marker so the report never implies a verified kickoff was observed;
      - all in-running (no pre-kickoff tick at all) → FAIL CLOSED: no decisions, no fabricated close;
      - incomplete close (a scored market the folded close fails to cover) → FAIL CLOSED. The fold
        GUARANTEES coverage by construction, so this is a defensive gate; :func:`pre_match_close_gap`
        names the offending markets when it ever fires.
"""

from __future__ import annotations

from dataclasses import dataclass

from veridex.ingest.marketstate import MarketState

#: Marker for a pre-match-only pack (never went in-running) — honest about the absent kickoff.
NO_TRANSITION_NOTE = (
    "no in-running transition observed: the fixture never went in-running in this pack, so the close is "
    "the final per-market pre-match line (no verified kickoff was seen — this is not a confirmed CON-040 close)."
)
#: Marker for a fixture already in-running at the first observed tick — no pre-kickoff evidence exists.
NO_PRE_KICKOFF_NOTE = (
    "fail-closed: the fixture was already in-running at the first observed tick, so there is no pre-kickoff "
    "line to reconstruct a CON-040 close from — pre-match CLV is not scored (no close was fabricated)."
)


@dataclass(frozen=True)
class PreMatchPlan:
    """The decision/close split for a ``pre_match`` backtest over one fixture's replayed ticks.

    Attributes:
        decision_states: The ordered pre-kickoff ticks fed as decision points (never an in-running tick).
        closing_state: The folded per-market CON-040 close to seal via ``feed_closing`` — ``None`` when
            no pre-kickoff close exists (a fail-closed degrade), so the caller MUST NOT feed it.
        degraded: ``True`` when a true pre-match close could NOT be produced (all-in-running / incomplete):
            the run must finalize on WINDOW CLV (never true ``clv_bps``) so no row overclaims a close.
        closing_note: An honest, human-readable provenance/degrade marker (``None`` only for a clean
            full-match pre_match with a verified kickoff and a complete close).
    """

    decision_states: list[MarketState]
    closing_state: MarketState | None
    degraded: bool
    closing_note: str | None


def first_inrunning_index(marketstates: list[MarketState]) -> int | None:
    """Index of the FIRST in-running (``phase == 1``) tick — the kickoff cutoff — or ``None`` if never.

    A single field keys the pre_match end rule (exactly as ``live_runner._is_in_running``): the FIRST
    ``phase == 1`` tick is kickoff. A later ``phase == 0`` after it does not reopen the window.
    """
    return next((i for i, state in enumerate(marketstates) if state.phase == 1), None)


def reconstruct_pre_match_close(states: list[MarketState]) -> MarketState | None:
    """Fold the LAST pre-kickoff (``phase == 0``) line PER market into ONE close snapshot, or ``None``.

    Mirrors ``live_runner._reconstruct_closing_state``'s completeness contract at the ``MarketState``
    level: a scored market whose value is NOT in the close would fall back (via
    ``orchestrator._closing_snapshots``) to its last-seen decision tick and silently score CLV 0. So the
    close carries EVERY market seen in a ``phase == 0`` tick, each at its OWN last pre-kickoff value.

    When a SINGLE pre-kickoff tick already carries every market, that real tick is returned UNCHANGED
    (identity) so the legacy pre-match-only path stays byte-identical; only a market whose last value
    lives on an EARLIER tick forces a synthesized fold (base tick + the earlier markets backfilled).

    Args:
        states: The ordered states to reconstruct the close over (the caller passes only pre-kickoff
            ticks; any non-``phase == 0`` state is ignored defensively).

    Returns:
        The folded closing :class:`MarketState`, or ``None`` when no ``phase == 0`` tick exists.
    """
    phase0 = [state for state in states if state.phase == 0]
    if not phase0:
        return None

    # Last pre-kickoff state carrying each market (later ticks overwrite earlier — CON-040 per market).
    last_by_market: dict[str, MarketState] = {}
    for state in phase0:  # ordered
        for market_key in state.markets:
            last_by_market[market_key] = state

    base = phase0[-1]  # the latest pre-kickoff line — its ts/tick_seq/scores anchor the close snapshot.
    missing = {mk: state for mk, state in last_by_market.items() if mk not in base.markets}
    if not missing:
        return base  # the last tick already covers everything — identity (byte-identical legacy path).

    merged = dict(base.markets)
    for market_key, state in missing.items():
        merged[market_key] = state.markets[market_key]  # backfill a market last priced on an earlier tick
    return base.model_copy(update={"markets": merged})


def pre_match_close_gap(decision_states: list[MarketState], closing_state: MarketState | None) -> set[str]:
    """Scored markets the close FAILS to cover — each a would-be silent CLV-0 fall-back (empty ⇒ safe).

    ``decision_states``' union of ``market_key``s is the conservative scored set (a superset of the
    actually-scored markets — an agent can only score a market it saw). Any such market missing from the
    close would fall back, via ``orchestrator._closing_snapshots``, to its last decision tick and score a
    silent CLV 0 while wearing a true-``clv_bps`` label. A ``None`` close leaves EVERY seen market uncovered.
    """
    seen: set[str] = set().union(*(set(state.markets) for state in decision_states)) if decision_states else set()
    if closing_state is None:
        return seen
    return seen - set(closing_state.markets)


def plan_pre_match_backtest(marketstates: list[MarketState]) -> PreMatchPlan:
    """Split a fixture's replayed ticks into pre-kickoff DECISIONS + a per-market CON-040 CLOSE.

    See the module docstring for the full contract. Never fabricates a kickoff or a close: the two
    fail-closed edges (all-in-running, incomplete close) return ``degraded=True`` with a named reason,
    and the never-in-running edge is allowed but marked so the report can't imply a verified kickoff.

    Args:
        marketstates: The fixture's ordered replayed ticks (pre-kickoff and, on a full-match pack,
            in-running ticks too).

    Returns:
        The :class:`PreMatchPlan` describing the decision set, the close to seal, and any degrade marker.
    """
    first_ir = first_inrunning_index(marketstates)

    # Already in-running at tick 0 → no pre-kickoff evidence exists at all. FAIL CLOSED (never fabricate).
    if first_ir == 0:
        return PreMatchPlan(decision_states=[], closing_state=None, degraded=True, closing_note=NO_PRE_KICKOFF_NOTE)

    # Never in-running → a pre-match-only pack. Allowed (still true CLV), but hold out the last tick as the
    # close proxy (matches the legacy pre-match-only replay contract) and MARK that no kickoff was verified.
    if first_ir is None:
        if len(marketstates) < 2:
            # Too few ticks to hold out a distinct close — feed all, no held-out close (degenerate, marked).
            return PreMatchPlan(
                decision_states=list(marketstates),
                closing_state=None,
                degraded=False,
                closing_note=NO_TRANSITION_NOTE,
            )
        decision_states = list(marketstates[:-1])
        closing_state = reconstruct_pre_match_close(marketstates)  # base = the held-out last pre-match tick
        if pre_match_close_gap(decision_states, closing_state):
            return PreMatchPlan(decision_states, closing_state, True, _incomplete_note(decision_states, closing_state))
        return PreMatchPlan(decision_states, closing_state, False, NO_TRANSITION_NOTE)

    # Normal full-match pre_match: decide on every STRICTLY pre-kickoff tick; a later phase-0 after the
    # first kickoff is ignored (first kickoff wins). Reconstruct the CON-040 close from those ticks only.
    decision_states = list(marketstates[:first_ir])
    closing_state = reconstruct_pre_match_close(decision_states)
    if closing_state is None:
        return PreMatchPlan(decision_states, None, True, NO_PRE_KICKOFF_NOTE)
    if pre_match_close_gap(decision_states, closing_state):
        return PreMatchPlan(decision_states, closing_state, True, _incomplete_note(decision_states, closing_state))
    return PreMatchPlan(decision_states, closing_state, False, None)


def _incomplete_note(decision_states: list[MarketState], closing_state: MarketState | None) -> str:
    """Name the markets an incomplete close fails to cover (defensive fail-closed reason)."""
    missing = sorted(pre_match_close_gap(decision_states, closing_state))
    return (
        f"fail-closed: the reconstructed pre-match close does not cover scored market(s) {missing}; "
        "degraded to window CLV so no row is scored against its own entry tick while claiming a true close."
    )
