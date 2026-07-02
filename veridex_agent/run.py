"""WD-3 / Phase-2D T20 — the decoupled standalone-run core (REQ-052 / REQ-2D-701 launch path).

Runs ONE agent through the full Veridex spine — ingest → agent → law → evidence/seal → proof →
self-verify → (optional) anchor — with NO competition/roster/ranking framing. It is THE single
runner seam (no parallel deploy path): the same core backs a replay run, a windowed LIVE run, and
the opt-in policy-gated EXECUTION LANE, by COMPOSING the lower-level pieces rather than duplicating
their composition:

  * replay  → ``run_competition`` for one agent + the shared manifest/score-root/proof helpers.
  * live    → ``run_live_window`` (T8), which already streams live TxLINE ticks in real time, seals
    the CON-040 close, finalizes, and builds the after-seal proof card / manifest / anchor. The
    standalone core CALLS it (does not re-implement the live seal→proof composition).
  * exec    → ``run_execution_lane`` (Phase-2B), run STRICTLY DOWNSTREAM of the seal for
    ``execution_mode != "paper"``. Its receipts are NON-SCORING (``evidence=false``): they never
    enter the sealed prefix and never alter the score/proof (SEC-004 / SEC-2D-401). The proof card
    is byte-structurally identical whether or not the lane ran.

Same trust boundary as the arena: the law recomputes the score, the proof card binds the sealed
evidence hash, and the anchored Memo payload == the manifest hash. Credentials (Solana keypair for
``anchor_memo``, TxLINE creds inside the live client, venue keys inside the adapter) are resolved
from typed config INSIDE their seams — never here. This module is an agent SHELL (it may
transitively reach the LLM agent path via the orchestrator); it is NOT import-audited as trust-path.

DRY_RUN/PAPER launch path (T20): ``dry_run`` routes through the lane with a deterministic offline
``FakeVenueAdapter``; real ``live_guarded`` (real venue, real money) is intentionally NOT wired here
(that + the six live_guarded correctness gates are T20b + operator).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from veridex.chain.anchor import anchor_memo, run_manifest_hash
from veridex.checks.build import build_check_results, build_performance_metrics, check_results_to_proof_block
from veridex.competition.events import EventType
from veridex.competition.models import AgentEntry
from veridex.ingest.marketstate import MarketState
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.competition import DEFAULT_CLUSTER, SCHEMA_VERSIONS
from veridex.runtime.live_runner import run_live_window
from veridex.runtime.orchestrator import Agent, RunResult, run_competition
from veridex.runtime.window import RunWindow
from veridex.scoring import score_run
from veridex.store import InMemoryStore
from veridex.verifier.proof_card import proof_card_from_run_result
from veridex.verifier.recompute import (
    fixture_or_window_id_from_events,
    manifest_from_run,
    recompute_score_root,
    verify_run,
)

if TYPE_CHECKING:  # annotation-only — keeps ``import veridex_agent.run`` free of the venue/store deps
    from veridex.competition.events import CompetitionEvent  # noqa: F401
    from veridex.store import Store
    from veridex.venues.base import VenueAdapter

# Execution-mode labels (mirror veridex.competition.models.ExecutionMode / execution.runner values).
_PAPER = "paper"
_DRY_RUN = "dry_run"


class StandaloneRunResult(BaseModel):
    """The full artifact bundle of one standalone agent run (WD-3 / T20).

    Attributes:
        run_id: The sealed run identifier.
        source_mode: ``"replay"`` or ``"live"``.
        execution_mode: The EFFECTIVE execution mode — the requested mode when the execution lane
            ran, else ``"paper"`` (no lane). A NON-SCORING label.
        scores: The single-agent ranked metric stack (one row).
        proof_card: The judge-visible proof card (lineage + checks + anchor). Byte-structurally
            identical whether or not the execution lane ran (receipts never enter it).
        manifest_hash: SHA-256 of the run manifest (the exact anchored Memo payload).
        anchor_status: ``"anchored"`` or ``"not_anchored"``.
        signature: The Solana tx signature when anchored, else ``None``.
        verified: The self-verify verdict over the produced proof.
        verify_report: The :class:`~veridex.verifier.recompute.VerifyReport` as a dict.
        receipts: NON-SCORING execution receipts (``evidence=false``), each labeled with its
            ``mode`` (``dry_run``). Empty in paper mode / when no lane ran. NEVER in the sealed
            prefix, NEVER a score/proof input (SEC-004 / SEC-2D-401).
        run_manifest: The launched-instance pin — ``config_hash`` + ``policy_hash`` + ``window`` +
            ``execution_mode`` + ``source_mode``. A NON-SCORING operational record binding this run
            to the exact agent instance that produced it (not part of the sealed manifest).
        ops: NON-SEALED operational annotations from the live runner (e.g. the ``closing_source``
            fallback marker). Empty on the replay path.
    """

    run_id: str
    source_mode: str
    execution_mode: str = _PAPER
    scores: list[dict[str, Any]]
    proof_card: dict[str, Any]
    manifest_hash: str
    anchor_status: str
    signature: str | None
    verified: bool
    verify_report: dict[str, Any]
    receipts: list[dict[str, Any]] = Field(default_factory=list)
    run_manifest: dict[str, Any] = Field(default_factory=dict)
    ops: dict[str, Any] = Field(default_factory=dict)


class _SealedRun(BaseModel):
    """The seal→proof bundle a run driver (replay or live) produces, before verify/exec/pin.

    Internal composition seam: both the replay driver (``run_competition`` + the shared manifest/
    proof helpers) and the live driver (``run_live_window``) fill this in, so the shared tail
    (verify → execution lane → instance pin → result) is written ONCE.
    """

    model_config = {"arbitrary_types_allowed": True}

    run: RunResult
    source_mode: str
    scores: list[dict[str, Any]]
    proof_card: dict[str, Any]
    manifest_hash: str
    anchor_status: str
    signature: str | None
    ops: dict[str, Any] = Field(default_factory=dict)


async def _seal_replay(
    marketstates: list[MarketState],
    agent: Agent,
    *,
    source_mode: str,
    anchor_fn: Callable[[str], Awaitable[str]] | None,
) -> _SealedRun:
    """Seal one REPLAY run + build its proof (byte-identical to the pre-T20 standalone core).

    Full arena parity — passes the freshly-built manifest/manifest_hash + anchor + source_mode to
    ``build_check_results`` EXACTLY as ``run_demo_competition`` does, so MANIFEST_BOUND and ANCHOR
    get real verdicts and a standalone-deployed agent's card reads identically to an in-competition
    one. This is the exact composition that shipped before T20; it is untouched so replay/paper stay
    byte-for-byte identical.
    """
    run = await run_competition(marketstates, [agent], source_mode=source_mode)
    scores = score_run(run)

    score_root = recompute_score_root(scores)
    manifest = manifest_from_run(
        run,
        fixture_or_window_id=fixture_or_window_id_from_events(run.run_events),
        score_root=score_root,
        schema_versions=dict(SCHEMA_VERSIONS),
    )
    manifest_hash = run_manifest_hash(manifest)

    if anchor_fn is None:
        anchor_status = "not_anchored"
        signature: str | None = None
    else:
        signature = await anchor_fn(manifest_hash)
        anchor_status = "anchored"

    anchor_block = {"status": anchor_status, "signature": signature, "cluster": DEFAULT_CLUSTER}
    checks = check_results_to_proof_block(
        build_check_results(
            scores=scores,
            run=run,
            manifest=manifest,
            manifest_hash=manifest_hash,
            anchor=anchor_block,
            source_mode=source_mode,
        )
    )
    proof_card = proof_card_from_run_result(
        run,
        checks=checks,
        metrics=build_performance_metrics(scores),
        anchor=anchor_block,
        schema_versions=dict(SCHEMA_VERSIONS),
    )
    return _SealedRun(
        run=run,
        source_mode=source_mode,
        scores=scores,
        proof_card=proof_card,
        manifest_hash=manifest_hash,
        anchor_status=anchor_status,
        signature=signature,
    )


async def _seal_live(
    window: RunWindow,
    agent: Agent,
    *,
    stream: AsyncIterator[MarketState] | None,
    fetch_updates: Callable[[int], Awaitable[list[dict[str, Any]]]] | None,
    event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None,
    anchor_fn: Callable[[str], Awaitable[str]] | None,
) -> _SealedRun:
    """Seal one windowed LIVE run by COMPOSING the T8 live runner (no re-implemented seal→proof).

    ``run_live_window`` already streams live TxLINE ticks in real time, seals the CON-040 close,
    finalizes, and builds the after-seal proof card / manifest / anchor — so the standalone core
    delegates the entire live composition to it rather than duplicating any of it. Tests inject
    ``stream`` + ``fetch_updates`` for a fully offline live run (ZERO network).
    """
    live = await run_live_window(
        window,
        [agent],
        stream=stream,
        fetch_updates=fetch_updates,
        event_sink=event_sink,
        anchor_fn=anchor_fn,
    )
    return _SealedRun(
        run=live.run,
        source_mode=live.run.source_mode,
        scores=live.scores,
        proof_card=live.proof_card,
        manifest_hash=live.manifest_hash,
        anchor_status=live.anchor_status,
        signature=live.signature,
        ops=dict(live.ops),
    )


def _default_adapter(execution_mode: str) -> VenueAdapter:
    """Pick the venue adapter for ``execution_mode`` when the caller injected none.

    ``dry_run`` → the deterministic offline :class:`~veridex.venues.sx_bet.FakeVenueAdapter`.
    Anything else → the config-gated :class:`~veridex.venues.sx_bet.SXBetAdapter` skeleton (its
    live methods raise until enabled). T20 wires ONLY the dry_run path; real ``live_guarded``
    venue wiring + the six correctness gates are T20b + operator. Imported lazily so
    ``import veridex_agent.run`` pulls in no venue module at load time.
    """
    from veridex.venues.sx_bet import FakeVenueAdapter, SXBetAdapter  # noqa: PLC0415  (lazy venue import)

    return FakeVenueAdapter() if execution_mode == _DRY_RUN else SXBetAdapter()


def _execution_event_ts(run: RunResult) -> int:
    """A deterministic event timestamp for the lane — the run's last sealed tick ``ts`` (else 0).

    Used to stamp the derived execution events and to compute quote age; deriving it from the
    sealed evidence (never wall-clock) keeps the lane deterministic and offline-friendly.
    """
    tss: list[int] = []
    for event in run.run_events:
        if event.get("event_type") != "tick":
            continue
        snapshot = event.get("state_snapshot_json")
        if not snapshot:
            continue
        import json  # noqa: PLC0415  (tiny, local — avoids a module-level json import in this shell)

        ts = json.loads(snapshot).get("ts")
        if isinstance(ts, int):
            tss.append(ts)
    return max(tss, default=0)


async def _run_execution_lane_for(
    sealed: _SealedRun,
    agent: Agent,
    *,
    policy_envelope: PolicyEnvelope,
    execution_mode: str,
    adapter: VenueAdapter | None,
    store: Store | None,
) -> list[dict[str, Any]]:
    """Compose the Phase-2B execution lane over the SEALED run and return NON-SCORING receipts.

    Builds the single-agent execution roster (the solo standalone agent is execution-eligible),
    picks/uses the venue adapter, and invokes ``run_execution_lane`` STRICTLY DOWNSTREAM of the
    seal. The lane reads ONLY the sealed ``score_rows`` (deterministic-law edge), never the LLM
    claim, and never mutates the run — so returning its receipts leaves ``sealed.scores`` /
    ``sealed.proof_card`` / the evidence hash byte-identical (SEC-004 / SEC-2D-401 / AC-2B-05).

    Returns:
        The receipt payload dicts from the emitted ``EXECUTION_RECEIPT`` events (each labeled with
        its execution ``mode``). Empty when no proposal cleared the policy gate.
    """
    from veridex.execution.runner import run_execution_lane  # noqa: PLC0415  (lazy — venue-adjacent import)

    lane_store = store if store is not None else InMemoryStore()
    lane_adapter = adapter if adapter is not None else _default_adapter(execution_mode)
    entries_by_agent = {
        agent.agent_id: AgentEntry(
            agent_id=agent.agent_id,
            owner="operator",
            strategy="standalone",
            model=None,
            proof_mode=agent.proof_mode,
            execution_eligibility=True,
        )
    }
    events: list[CompetitionEvent] = await run_execution_lane(
        lane_store,
        competition_id=sealed.run.run_id,  # solo run — no competition container; the run id anchors it
        run_result=sealed.run,
        envelope=policy_envelope,
        adapter=lane_adapter,
        entries_by_agent=entries_by_agent,
        execution_mode=execution_mode,
        base_seq=0,
        event_ts=_execution_event_ts(sealed.run),
    )
    return [event.payload for event in events if event.event_type == EventType.EXECUTION_RECEIPT]


def _window_pin(window: RunWindow | None) -> dict[str, Any] | None:
    """The NON-SCORING window projection recorded in the run manifest (or ``None`` for replay)."""
    if window is None:
        return None
    return {
        "window_id": window.window_id,
        "fixture_id": window.fixture_id,
        "market_allowlist": list(window.market_allowlist),
        "end_rule": window.end_rule,
        "duration_s": window.duration_s,
        "min_clv_horizon_s": window.min_clv_horizon_s,
    }


async def standalone_run(
    marketstates: list[MarketState],
    agent: Agent,
    *,
    source_mode: str = "replay",
    window: RunWindow | None = None,
    stream: AsyncIterator[MarketState] | None = None,
    fetch_updates: Callable[[int], Awaitable[list[dict[str, Any]]]] | None = None,
    policy_envelope: PolicyEnvelope | None = None,
    execution_mode: str = _DRY_RUN,
    adapter: VenueAdapter | None = None,
    execution_store: Store | None = None,
    config_hash: str | None = None,
    event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    anchor_fn: Callable[[str], Awaitable[str]] | None = anchor_memo,
) -> StandaloneRunResult:
    """Run ONE agent through the full spine and return a self-verified, optionally-anchored proof.

    Dispatches on ``window``: a windowed LIVE run (``window`` set) composes the T8 live runner over
    an injected/real stream; otherwise a REPLAY run over ``marketstates`` (byte-identical to the
    pre-T20 core). When a ``policy_envelope`` is supplied and ``execution_mode != "paper"``, the
    Phase-2B execution lane runs STRICTLY DOWNSTREAM of the seal and its NON-SCORING receipts are
    surfaced on the result — the sealed evidence / score / proof are unchanged either way.

    Args:
        marketstates: Ordered tick snapshots for a REPLAY run (ignored on the live path — the
            stream provides ticks).
        agent: The single :class:`~veridex.runtime.orchestrator.Agent` to run.
        source_mode: ``"replay"`` or ``"live"`` for the replay driver (the live path always seals
            ``"live"`` regardless).
        window: When set, run the windowed LIVE path via ``run_live_window`` (the launch path).
        stream: Injected async iterator of :class:`MarketState` for the live path (tests). ``None``
            → the real ``stream_marketstates`` (lazy httpx) inside the live runner.
        fetch_updates: Injected ``async (fixture_id) -> updates`` for the CON-040 close (tests).
        policy_envelope: The operator guardrails. When set AND ``execution_mode != "paper"``, the
            execution lane runs; otherwise it is skipped (paper is proof-only).
        execution_mode: ``"paper"`` | ``"dry_run"`` | ``"live_guarded"``. Defaults to ``dry_run``
            (the safe non-paper default); the lane only engages when a ``policy_envelope`` is given.
        adapter: Injected venue adapter for the lane; ``None`` → picked by ``execution_mode``
            (``dry_run`` → offline ``FakeVenueAdapter``).
        execution_store: Injected store for the lane's execution records; ``None`` → a private
            in-memory store (the standalone core has no competition log to persist to).
        config_hash: The non-secret agent-config hash pinned into the run manifest (the launched
            instance). ``None`` when the caller did not compute one.
        event_sink: Optional async observer forwarded to the live runner (projection only).
        anchor_fn: Injectable ``async (manifest_hash) -> signature``; ``None`` skips anchoring.
            Defaults to the real :func:`~veridex.chain.anchor.anchor_memo`.

    Returns:
        The :class:`StandaloneRunResult` bundle.
    """
    if window is not None:
        sealed = await _seal_live(
            window,
            agent,
            stream=stream,
            fetch_updates=fetch_updates,
            event_sink=event_sink,
            anchor_fn=anchor_fn,
        )
        report = verify_run(sealed.run, fixture_or_window_id=window.window_id)
    else:
        sealed = await _seal_replay(marketstates, agent, source_mode=source_mode, anchor_fn=anchor_fn)
        report = verify_run(sealed.run)

    # --- opt-in EXECUTION LANE — downstream of the seal, NON-SCORING (SEC-004 / SEC-2D-401) -------
    # A paper run (or one without an envelope) is proof-only: no lane, no receipts, and the reported
    # execution_mode collapses to "paper" (the honest EFFECTIVE mode).
    receipts: list[dict[str, Any]] = []
    ops = dict(sealed.ops)
    ran_lane = policy_envelope is not None and execution_mode != _PAPER
    if policy_envelope is not None and ran_lane:
        # FAILURE ISOLATION (same doctrine as the T8 interrupt-degrade): the lane runs STRICTLY
        # downstream of the seal + verify + anchor, so a lane exception (e.g. a not-yet-wired
        # live_guarded adapter raising, or any injected adapter that fails) must NEVER vaporize the
        # already-built — possibly already-ANCHORED-ON-CHAIN — proof. Preserve and return the sealed
        # result with NO receipts, recording the cause honestly under the non-sealed ``ops`` channel
        # (never silently swallowed). The proof stays byte-identical to the no-lane run.
        try:
            receipts = await _run_execution_lane_for(
                sealed,
                agent,
                policy_envelope=policy_envelope,  # narrowed non-None by the guard above
                execution_mode=execution_mode,
                adapter=adapter,
                store=execution_store,
            )
        except Exception as exc:  # noqa: BLE001 — downstream, non-scoring lane; the seal must survive it.
            receipts = []
            ops["execution_lane_error"] = f"{type(exc).__name__}: {exc}"
    effective_execution_mode = execution_mode if ran_lane else _PAPER

    # --- agent-instance pin (NON-SCORING) — config_hash + policy_hash + window + modes -----------
    run_manifest = {
        "config_hash": config_hash,
        "policy_hash": policy_envelope.policy_hash() if policy_envelope is not None else None,
        "execution_mode": effective_execution_mode,
        "source_mode": sealed.source_mode,
        "window": _window_pin(window),
    }

    return StandaloneRunResult(
        run_id=sealed.run.run_id,
        source_mode=sealed.source_mode,
        execution_mode=effective_execution_mode,
        scores=sealed.scores,
        proof_card=sealed.proof_card,
        manifest_hash=sealed.manifest_hash,
        anchor_status=sealed.anchor_status,
        signature=sealed.signature,
        verified=report.verified,
        verify_report=report.model_dump(),
        receipts=receipts,
        run_manifest=run_manifest,
        ops=ops,
    )
