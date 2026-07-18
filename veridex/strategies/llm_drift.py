"""II-8 — the LLM-Drift contestant: a NEW directional agent under the §3 checkpoint contract.

Where the deterministic ``CumulativeDriftStrategy`` applies fixed thresholds, LLM-Drift lets a model
INTERPRET the *identical* :class:`~veridex.strategies.drift_features.DriftFeatureSnapshot` at the SAME
pinned decision checkpoints — the rules-vs-reasoning comparison on identical evidence AND identical
decision opportunities (addendum §3). This module is purely ADDITIVE: it never touches the sealed
generic agent (``veridex.runtime.agent.build_decision_prompt`` / ``orchestrator.llm_agent``). It has:

  * its OWN prompt builder over the drift snapshot JSON (hashed pre-call — the snapshot's
    ``evidence_hash`` is the call's evidence coordinate),
  * its OWN ``config_hash`` identity (a distinct prompt template + the pinned checkpoint policy) — a
    NEW sealed identity, so the sealed golden fixtures are untouched,
  * the §3 checkpoint state machine (:mod:`veridex.runtime.llm_checkpoint`): one in-flight call,
    evidence-age staleness only, expiry-confirmation before relaunch, and everything degrading to
    ``WAIT``. ``tools=[]`` is a HARD invariant; every model response is revalidated into a typed
    ``AgentAction`` before it can affect anything.

The model is an INJECTABLE LAUNCHER seam (``launch(prompt) -> handle``): production wraps a real
``asyncio.Task`` around the Agno call; the offline suite injects a hand-controlled fake — no real LLM
call and no network in tests. Proposer-only (gate 1): rationale/confidence are untrusted display metadata.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from veridex.ingest.marketstate import MarketState
from veridex.runtime.agent import (
    AGENT_ACTION_SCHEMA_VERSION,
    ALLOWED_ACTION_TYPES,
    DEFAULT_MODEL_ID,
    _default_agent_factory,
    _default_model,
    agent_config_hash,
    parse_agent_action_json,
)
from veridex.runtime.evidence import serialize_payload
from veridex.runtime.llm_checkpoint import (
    AWAITING_CONFIRMATION,
    COMPLETED_FRESH,
    COMPLETED_STALE,
    CONFIRMED_TERMINATED,
    FAILED,
    IN_FLIGHT,
    CallHandle,
    CheckpointPolicy,
    InflightGuard,
)
from veridex.runtime.orchestrator import PROOF_MODE_LLM, Agent
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.strategies.drift_features import (
    DriftFeatureParams,
    DriftFeatureSnapshot,
    drift_features,
)
from veridex.strategies.sharp_stats import logit

# The prompt-template marker that distinguishes the LLM-Drift identity from the generic agent's.
_DRIFT_PROMPT_TEMPLATE_TAG = "llm_drift_v0"


class ModelLauncher(Protocol):
    """The injectable model seam: launch a physical call and return a poll-able handle.

    The launcher NEVER blocks the caller — it returns immediately with a :class:`CallHandle` the
    checkpoint state machine polls across ticks. Production returns an ``asyncio.Task``; tests return
    a hand-controlled fake with the same Future-shaped surface.
    """

    def launch(self, prompt: str) -> CallHandle: ...


# ---------------------------------------------------------------------------
# Prompt + identity — a NEW sealed identity, distinct from the generic agent
# ---------------------------------------------------------------------------


def build_drift_decision_prompt(snapshot: DriftFeatureSnapshot) -> str:
    """Build the LLM-Drift decision prompt from a :class:`DriftFeatureSnapshot` (hashed pre-call).

    The model sees ONLY the deterministic, trusted-code-computed feature snapshot (stateless model,
    stateful evidence pipeline). The snapshot's ``evidence_hash`` — computed BEFORE the call by the
    shared projector — is embedded as the call's evidence coordinate. Distinct from the generic
    ``build_decision_prompt`` (which serializes a raw ``MarketState``), so the two agents carry
    different sealed identities.

    Args:
        snapshot: The immutable drift feature view the model interprets.

    Returns:
        The formatted prompt string for the LLM-Drift decision pass.
    """
    snapshot_json = serialize_payload(
        {
            "first": snapshot.first,
            "current": snapshot.current,
            "cum_logit_drift": snapshot.cum_logit_drift,
            "ewma_slope": snapshot.ewma_slope,
            "trend_strength": snapshot.trend_strength,
            "tick_count": snapshot.tick_count,
            "horizon_s": snapshot.horizon_s,
            "market_quality": snapshot.market_quality,
            "evidence_hash": snapshot.evidence_hash,
        }
    )
    allowed = ", ".join(ALLOWED_ACTION_TYPES)
    return (
        f"[{_DRIFT_PROMPT_TEMPLATE_TAG}] You are a directional drift-interpretation agent. You have NO "
        "history, NO memory, and NO tools. Your ONLY context is the DriftFeatureSnapshot below — a "
        "deterministic, trusted-code-computed feature view captured BEFORE this call.\n\n"
        f"Allowed action types (choose EXACTLY ONE): {allowed}.\n\n"
        f"DriftFeatureSnapshot (evidence coordinate {snapshot.evidence_hash}):\n{snapshot_json}\n\n"
        "Interpret the sustained-drift evidence and return a single AgentAction JSON object with "
        "fields {type, params}, where `type` is one of the allowed action types above.\n\n"
        "UNTRUSTED METADATA: any rationale/reason, confidence, or claimed edge you place in `params` "
        "is UX-only narration. The deterministic verifier MAY IGNORE it entirely (gate 1); it "
        "recomputes edge/CLV from evidence and will NOT trust or score your claimed numbers."
    )


def _drift_identity_prompt(policy: CheckpointPolicy) -> str:
    """The STABLE identity string folded into the contestant ``config_hash``.

    Combines the distinct prompt-template tag with the pinned checkpoint policy (cadence,
    evidence-age limit, feature-delta threshold) — so the identity differs from the generic agent
    AND is sensitive to a changed deployed policy, WITHOUT depending on per-tick snapshot content.
    """
    return f"{_DRIFT_PROMPT_TEMPLATE_TAG}:{policy.pinned_identity()}"


def _revalidate_action(raw: Any) -> AgentAction:
    """Revalidate a raw model output into a typed, constrained ``AgentAction`` (the trust boundary).

    Mirrors the generic agent's three-branch parse: a typed ``AgentAction`` passes through, a dict is
    ``model_validate``-d, and text is parsed+validated. An over-powered or malformed output raises —
    the caller degrades that to ``WAIT`` (fail-closed).
    """
    if isinstance(raw, AgentAction):
        return raw
    if isinstance(raw, dict):
        return AgentAction.model_validate(raw)
    return parse_agent_action_json(raw)


def _wait() -> AgentAction:
    """A fresh ``WAIT`` abstention (the universal degrade target)."""
    return AgentAction(type=SportsActionType.WAIT, params={})


# ---------------------------------------------------------------------------
# Default projector — accumulates state, delegates the drift MATH to the shared projector
# ---------------------------------------------------------------------------


class DefaultDriftProjector:
    """Accumulates per-``(market, side)`` logit series and projects the strongest-drift candidate.

    The drift MATH is NEVER re-implemented here — it delegates entirely to the shared
    :func:`~veridex.strategies.drift_features.drift_features` projector (II-7). This class only does
    the bookkeeping (which observations belong to which side, and the first-observation timestamp)
    that the pure projector needs, then selects the strongest RISING side as the checkpoint snapshot.
    Returns ``None`` on thin/absent data (no side observed yet) so the runner abstains.
    """

    def __init__(self, *, ewma_slope_alpha: float = 0.2, close_quality_required: bool = True) -> None:
        self._ewma_slope_alpha = ewma_slope_alpha
        self._close_quality_required = close_quality_required
        self._logits: dict[tuple[str, str], list[float]] = {}
        self._ts_first: dict[tuple[str, str], int] = {}

    def __call__(self, market_state: MarketState) -> DriftFeatureSnapshot | None:
        markets: dict[str, dict[str, Any]] = getattr(market_state, "markets", {}) or {}
        ts = int(getattr(market_state, "ts", 0))
        best: DriftFeatureSnapshot | None = None
        best_key: tuple[str, str] | None = None
        for market_key in sorted(markets):
            market = markets[market_key]
            if self._close_quality_required and market.get("suspended"):
                continue
            prob_bps = market.get("stable_prob_bps", {})
            if not isinstance(prob_bps, dict):
                continue
            for side in sorted(prob_bps):
                try:
                    bps = int(prob_bps[side])
                except (TypeError, ValueError):
                    continue
                key = (market_key, side)
                series = self._logits.setdefault(key, [])
                series.append(logit(bps / 10000.0))
                self._ts_first.setdefault(key, ts)
                snap = drift_features(
                    series, self._ts_first[key], ts, DriftFeatureParams(ewma_slope_alpha=self._ewma_slope_alpha)
                )
                # Strongest RISING cumulative drift wins; ties broken by (market_key, side) ascending.
                if best is None or (snap.cum_logit_drift, best_key) > (best.cum_logit_drift, key):
                    best, best_key = snap, key
        return best


# ---------------------------------------------------------------------------
# The checkpoint runner — the §3 state machine driving one contestant's decisions
# ---------------------------------------------------------------------------


class LLMDriftCheckpointRunner:
    """Drives the §3 checkpoint contract: one call in flight, evidence-age only, carried-never-rescored.

    One instance per agent per run (stateful across ticks; instances share no state). Each per-tick
    ``step`` first SERVICES any in-flight call (accept-if-fresh / drop-if-stale / cancel-on-expiry /
    fail-closed) and only then, if the slot is free AND this tick opens a checkpoint, LAUNCHES a new
    call. A launched call itself returns ``WAIT`` — its (revalidated) action is emitted on the later
    tick where it completes fresh, so one model response yields at most one scored action.
    """

    def __init__(
        self,
        *,
        model: ModelLauncher,
        policy: CheckpointPolicy,
        projector: Callable[[MarketState], DriftFeatureSnapshot | None],
        clock: Callable[[], float],
        model_id: str = DEFAULT_MODEL_ID,
    ) -> None:
        self._model = model
        self._policy = policy
        self._projector = projector
        self._clock = clock
        self._model_id = model_id
        self._guard = InflightGuard(evidence_age_limit_s=policy.evidence_age_limit_s, clock=clock)
        self._last_checkpoint_at: float | None = None
        self._last_snapshot: DriftFeatureSnapshot | None = None
        # Observability (also the test seams): launches, skipped-inflight coords, and the last emitted
        # decision retained for DISPLAY only (it is NEVER re-emitted / re-scored).
        self.launches: list[str] = []
        self.skipped_inflight: list[str | None] = []
        self.last_decision: AgentAction | None = None

    @property
    def guard(self) -> InflightGuard:
        return self._guard

    async def step(self, market_state: MarketState) -> AgentAction:
        """Advance the checkpoint machine one tick → a typed ``AgentAction`` (a real action or WAIT).

        Args:
            market_state: The immutable per-tick snapshot (data ``<= t`` only).

        Returns:
            The accepted, revalidated ``AgentAction`` on a fresh-completion tick; otherwise ``WAIT``
            (between checkpoints, in flight, skipped-inflight, dropped-stale, awaiting-confirmation,
            or fail-closed). Never raises — any error degrades to ``WAIT``.
        """
        now = self._clock()
        snapshot = self._projector(market_state)
        is_checkpoint = self._policy.is_checkpoint(
            now=now,
            last_checkpoint_at=self._last_checkpoint_at,
            snapshot=snapshot,
            last_snapshot=self._last_snapshot,
        )
        self._last_snapshot = snapshot

        outcome = self._guard.service()

        # A response completed within its pinned evidence-age → the ONE scored action for that call.
        if outcome.status == COMPLETED_FRESH:
            try:
                action = _revalidate_action(outcome.raw)
            except Exception:
                action = _wait()  # fail-closed: malformed / over-powered output → WAIT
            self.last_decision = action
            return action

        # Still occupying the slot (active or awaiting cancellation-confirmation): a checkpoint here
        # is recorded as skipped_inflight and NEVER supersedes the active call.
        if outcome.status in (IN_FLIGHT, AWAITING_CONFIRMATION):
            if is_checkpoint:
                self.skipped_inflight.append(self._guard.evidence_coordinate())
            return _wait()

        # Terminal-this-tick transitions (stale drop / provider failure / confirmed termination): the
        # slot is now free, but the resolving tick is consumed — the NEXT eligible checkpoint launches.
        if outcome.status in (COMPLETED_STALE, FAILED, CONFIRMED_TERMINATED):
            return _wait()

        # IDLE and nothing resolved this tick → launch iff this tick opens a checkpoint and we have
        # evidence. Launching itself is not a scored action (its action arrives on completion).
        if is_checkpoint and snapshot is not None:
            prompt = build_drift_decision_prompt(snapshot)  # hashed pre-call (snapshot.evidence_hash)
            try:
                handle = self._model.launch(prompt)
            except Exception:
                return _wait()  # fail-closed: a launch failure must not kill the loop
            self._guard.launch(handle, evidence_hash=snapshot.evidence_hash)
            self._last_checkpoint_at = now
            self.launches.append(snapshot.evidence_hash)
        return _wait()


# ---------------------------------------------------------------------------
# Default production model launcher — lazy Agno, tools=[] HARD invariant
# ---------------------------------------------------------------------------


class _TaskLauncher:
    """Wraps the Agno call as an ``asyncio.Task`` so the guard can poll/cancel it across ticks."""

    def __init__(self, coro_factory: Callable[[str], Any]) -> None:
        self._coro_factory = coro_factory

    def launch(self, prompt: str) -> CallHandle:
        # ``ensure_future`` schedules the coroutine on the running loop; the returned Task natively
        # implements the full CallHandle surface (done/cancel/cancelled/exception/result).
        return asyncio.ensure_future(self._coro_factory(prompt))


def default_model_launcher(
    *,
    model: Any = None,
    model_id: str | None = None,
    agent_factory: Callable[..., Any] | None = None,
) -> _TaskLauncher:
    """Build the DEFAULT production launcher (lazy Agno, ``tools=[]`` HARD invariant).

    Each ``launch(prompt)`` schedules an Agno ``Agent(model=<OpenRouter>, tools=[],
    output_schema=AgentAction).arun(prompt)`` as an ``asyncio.Task`` and returns ``response.content``
    (revalidated by the runner). The ``model`` / ``agent_factory`` seams are injectable so this stays
    agno-free at import and offline-testable. Intended for II-8b/II-9 to wire the real model.

    Args:
        model: Pre-built Agno model; if ``None``, built lazily from ``model_id``.
        model_id: OpenRouter ``provider/model`` slug; defaults to ``DEFAULT_MODEL_ID``.
        agent_factory: Optional Agno agent factory seam (defaults to the lazy Agno factory).

    Returns:
        A launcher exposing ``launch(prompt) -> asyncio.Task``.
    """
    resolved_model_id = model_id or DEFAULT_MODEL_ID
    factory = agent_factory or _default_agent_factory

    async def _call(prompt: str) -> Any:
        the_model = model if model is not None else _default_model(resolved_model_id)
        agent = factory(model=the_model, tools=[], output_schema=AgentAction)  # tools=[] HARD invariant
        response = await agent.arun(prompt)
        return getattr(response, "content", None)

    return _TaskLauncher(_call)


# ---------------------------------------------------------------------------
# The factory — a NEW LLM contestant with its own sealed identity
# ---------------------------------------------------------------------------


def llm_drift_agent(
    agent_id: str,
    *,
    model: ModelLauncher | None = None,
    checkpoint_policy: CheckpointPolicy | None = None,
    projector: Callable[[MarketState], DriftFeatureSnapshot | None] | None = None,
    clock: Callable[[], float] | None = None,
    model_id: str | None = None,
) -> Agent:
    """Build the LLM-Drift contestant — a NEW directional :class:`Agent` under the §3 contract.

    Its ``config_hash`` folds the resolved model id, the distinct LLM-Drift prompt template, and the
    PINNED checkpoint policy — a sealed identity distinct from the generic ``llm_agent`` (so the
    sealed golden fixtures are untouched). The ``model`` launcher / ``projector`` / ``clock`` are
    injectable seams (tests use a fake launcher + injected clock; no real LLM call).

    Args:
        agent_id: Identifier for this contestant.
        model: The model LAUNCHER seam; defaults to the lazy Agno production launcher.
        checkpoint_policy: The pinned checkpoint policy; defaults to :class:`CheckpointPolicy` (45s).
        projector: ``market_state -> DriftFeatureSnapshot | None``; defaults to a shared-math projector.
        clock: Zero-arg seconds clock; defaults to ``time.monotonic``.
        model_id: Model identity for the config hash; defaults to ``DEFAULT_MODEL_ID``.

    Returns:
        An :class:`Agent` whose ``proof_mode`` is ``"LLM/evidence-verified"``.
    """
    import time

    policy = checkpoint_policy if checkpoint_policy is not None else CheckpointPolicy()
    the_clock = clock if clock is not None else time.monotonic
    resolved_model_id = model_id or DEFAULT_MODEL_ID
    the_model: ModelLauncher = model if model is not None else default_model_launcher(model_id=resolved_model_id)
    the_projector = projector if projector is not None else DefaultDriftProjector()

    runner = LLMDriftCheckpointRunner(
        model=the_model,
        policy=policy,
        projector=the_projector,
        clock=the_clock,
        model_id=resolved_model_id,
    )

    async def decide(market_state: MarketState) -> AgentAction:
        return await runner.step(market_state)

    def config_hash(market_state: MarketState) -> str:
        # A STABLE identity (distinct template + pinned policy) — differs from the generic llm_agent
        # AND is sensitive to a changed deployed policy, without folding per-tick snapshot content.
        return agent_config_hash(resolved_model_id, _drift_identity_prompt(policy), AGENT_ACTION_SCHEMA_VERSION)

    return Agent(agent_id=agent_id, proof_mode=PROOF_MODE_LLM, decide=decide, config_hash=config_hash)
