"""FU-3 — wrap each deterministic baseline as an orchestrator ``Agent`` so the SAME law/scoring seam
that scores drift also scores the baselines.

Before FU-3 the baselines were called DIRECTLY (``evaluation._baseline_action``) and their rows carried
``clv_bps`` hardcoded ``None`` — null references, never scored competitors. Here each baseline becomes a
per-tick :class:`~veridex.runtime.orchestrator.Agent` that the producer runs through
:func:`~veridex.backtest.runner.run_backtest` exactly like ``cumulative_drift_agent`` — so its fired pick
is scored by :func:`veridex.law.recompute` against the per-market CON-040 kickoff close, the IDENTICAL
path (and identical close) drift is scored through. No CLV is computed here; the law owns scoring.

D2-consistent decision/close split (the FU-1 NIT, now load-bearing): ``run_backtest`` feeds ONLY the
pre-kickoff ``decision_states`` to ``decide()`` and supplies the folded close via ``feed_closing`` (never
a decision tick). So a baseline agent decides purely from the pre-kickoff series and can NEVER peek at the
close it is scored against — no lookahead, by construction (this is why a baseline's flipped action is now
a deliberate, tested property rather than an undocumented delta). ``no_trade`` always abstains (WAIT) → it
is never scored and stays null; a fired pick with no valid close FAILS CLOSED inside the runner (the row
degrades to WINDOW CLV, so :func:`veridex.scoring.is_scored` is ``False`` → ``clv_bps`` projects to
``None``) — never a fabricated CLV 0.

The baselines keep their heterogeneous decision RULES verbatim (:data:`veridex.backtest.baselines.BASELINES`);
this module only adapts them to the per-tick ``decide`` contract and injects the ``market_key`` + ``side``
the law needs to score the pick. The per-tick inputs (the chosen market, its ``side0`` price series, the
current fair-prob map, a running horizon) mirror the OLD ``evaluation._baseline_inputs`` derivation, but
sourced incrementally from the decision stream rather than peeked from the close.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from veridex.backtest.baselines import BASELINES
from veridex.ingest.marketstate import MarketState
from veridex.runtime.agent import AGENT_ACTION_SCHEMA_VERSION, agent_config_hash
from veridex.runtime.orchestrator import PROOF_MODE_REPRODUCIBLE, Agent
from veridex.runtime.schemas import AgentAction, SportsActionType


def _dispatch(name: str, fn: Callable, *, prices: list[float], fair_probs: dict[str, float], horizon_s: int, seed: int) -> AgentAction | None:
    """Call ONE baseline with ITS OWN signature (DRIFT-1); ``None`` for an unknown baseline shape.

    Mirrors the retired ``evaluation._baseline_action`` dispatch so each baseline's decision rule is used
    verbatim — the adapter only supplies the per-tick inputs, never a reinvented rule.
    """
    if name == "no_trade":
        return fn(prices, horizon_s)
    if name == "favorite":
        return fn(fair_probs, horizon_s)
    if name == "threshold_move":
        return fn(prices, horizon_s)
    if name == "seeded_random":
        return fn(prices, horizon_s, seed)
    return None  # a baseline whose signature the adapter doesn't know — never guess


class BaselineAgentStrategy:
    """Stateful per-tick adapter turning a pure baseline rule into a scorable, fire-once agent decision.

    Per run it latches the first usable ``(market_key, side0)`` seen on the DECISION stream, accumulates
    ``side0``'s decimal-price series, and on each tick calls the underlying baseline with the inputs SEEN
    SO FAR. The first tick whose baseline decision is a fired pick is emitted as ``FOLLOW_MOMENTUM``
    carrying ``market_key`` + ``side`` (so the law scores it); thereafter the agent latches to ``WAIT`` —
    a baseline takes ONE position, giving exactly one scored row per fixture (vs the CON-040 close).

    Instances share NO state; decisions integrate only ticks already fed, so the agent is deterministic
    and causal (no lookahead) — the same reproducibility contract the drift agent holds.
    """

    def __init__(self, name: str, fn: Callable, *, seed: int) -> None:
        self._name = name
        self._fn = fn
        self._seed = seed
        self._market_key: str | None = None
        self._side0: str | None = None
        self._prices: list[float] = []
        self._first_ts: int | None = None
        self._fired = False

    def _observe(self, market_state: MarketState) -> dict[str, int] | None:
        """Fold one tick into state; return the latched market's current ``stable_prob_bps`` map, or None.

        Latches ``market_key`` to the first market (sorted, deterministic) carrying a USABLE (non-empty)
        prob map, and ``side0`` to that map's first side — the same data-usability selection the old
        ``_baseline_inputs`` used, but from the decision stream instead of the close. Returns ``None``
        while no usable market has been seen (a thin/degenerate tick → the agent abstains).
        """
        markets: dict[str, dict[str, Any]] = getattr(market_state, "markets", {}) or {}
        if self._market_key is None:
            for candidate_key in sorted(markets):
                probs = markets[candidate_key].get("stable_prob_bps") or {}
                if probs:
                    self._market_key = candidate_key
                    self._side0 = sorted(probs)[0]
                    break
        if self._market_key is None or self._side0 is None:
            return None

        market = markets.get(self._market_key, {})
        prob_bps = market.get("stable_prob_bps") or {}
        price_map = market.get("stable_price") or {}
        # Accumulate side0's decimal price, falling back to the fair-prob-derived price (mirrors the old
        # _baseline_inputs price series) so threshold_move/seeded_random see a real move series.
        if self._side0 in price_map:
            self._prices.append(float(price_map[self._side0]))
        elif self._side0 in prob_bps and prob_bps[self._side0]:
            self._prices.append(10000.0 / float(prob_bps[self._side0]))
        if self._first_ts is None:
            self._first_ts = int(getattr(market_state, "ts", 0))
        return prob_bps if prob_bps else None

    def decide(self, market_state: MarketState) -> AgentAction:
        """Observe this tick, then emit the baseline's fired pick ONCE (scorable) or ``WAIT``."""
        prob_bps = self._observe(market_state)
        if self._fired or self._market_key is None or self._side0 is None or prob_bps is None:
            return AgentAction(type=SportsActionType.WAIT, params={})

        fair_probs = {side: float(bps) / 10000.0 for side, bps in prob_bps.items()}
        horizon_s = int(getattr(market_state, "ts", 0)) - (self._first_ts or 0)
        action = _dispatch(
            self._name, self._fn, prices=self._prices, fair_probs=fair_probs, horizon_s=horizon_s, seed=self._seed
        )
        if action is None or action.type == SportsActionType.WAIT:
            return AgentAction(type=SportsActionType.WAIT, params={})

        # Enrich the fired pick with the market_key + side the law scores on. favorite names its own side
        # (the highest-fair-prob favorite); threshold_move/seeded_random carry no side → attribute to
        # side0, the same side whose price series drove their trigger.
        side = action.params.get("side") or self._side0
        self._fired = True
        return AgentAction(
            type=SportsActionType.FOLLOW_MOMENTUM,
            params={
                "market_key": self._market_key,
                "side": side,
                # UNTRUSTED UX metadata (gate 1) — never scored by the law:
                "reason": action.params.get("reason", f"baseline: {self._name}"),
            },
        )

    async def adecide(self, market_state: MarketState) -> AgentAction:
        """Async wrapper over :meth:`decide` (the orchestrator gathers ``async`` deciders)."""
        return self.decide(market_state)


def baseline_agent(name: str, *, seed: int) -> Agent:
    """Build a reproducible-proof :class:`Agent` wrapping the named baseline for the SAME scored path drift uses.

    The agent's ``agent_id`` is the baseline ``name`` so the producer can split a shared run's score rows
    back to per-baseline ``kind`` rows. Proposer-only (gate 1): it emits ``FOLLOW_MOMENTUM``/``WAIT`` and
    the deterministic law scores CLV — the baseline self-certifies nothing.

    Args:
        name: A baseline name present in :data:`veridex.backtest.baselines.BASELINES`.
        seed: The per-fixture seed (the producer passes ``fixture_id``) — bound into the config hash so a
            run is reproducible purely from its config, matching the drift agent's contract.

    Returns:
        An :class:`Agent` whose ``proof_mode`` is ``"reproducible"``.

    Raises:
        KeyError: If ``name`` is not a known baseline.
    """
    fn = BASELINES[name]
    strategy = BaselineAgentStrategy(name, fn, seed=seed)

    async def decide(market_state: MarketState) -> AgentAction:
        return await strategy.adecide(market_state)

    def config_hash(market_state: MarketState) -> str:
        return agent_config_hash(name, f"baseline:{name}:seed={seed}", AGENT_ACTION_SCHEMA_VERSION)

    return Agent(agent_id=name, proof_mode=PROOF_MODE_REPRODUCIBLE, decide=decide, config_hash=config_hash)
