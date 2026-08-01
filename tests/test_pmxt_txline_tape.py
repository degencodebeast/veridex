"""RED suite for the ``pmxt-txline-mm-18209181-v1`` REAL-DATA maker replay tape.

This tape is the provenance-correct real-data artifact: REAL Polymarket 10-level order-book depth
(``.depth`` slice) + REAL TxLINE 1X2 fair value (a content-hash-verified ReplayPack sub-slice),
FIFA World Cup France v Morocco (fixture ``18209181``), replayed dry-run. The suite proves the five
defects of the parked SX hybrid tape are each fixed, plus the honest-builder / cadence-authentication
/ no-look-ahead / Studio-payload-E2E claims.

Every test names the Major (M1..M5) it guards, or the honesty/authentication invariant it pins.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from veridex.api.deploy import DeployDeps, register_deploy_routes
from veridex.config import Settings
from veridex.deploy.instance import DeployStatus
from veridex.ingest.replay_pack import load_pack_marketstates, verify_content_hash
from veridex.live_recorder.contracts import LiveRecorderSessionMeta
from veridex.live_recorder.recorder import LiveRecorder
from veridex.mm_strategy import pmxt_tape
from veridex.mm_strategy import session_factory as sf
from veridex.mm_strategy.assembler import FvArrival, ObservationTick, run_cadence
from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import StrategyState
from veridex.mm_strategy.core import decide
from veridex.mm_strategy.offline_proposer import OfflineRecordingProposer
from veridex.runtime.runtime_events import RuntimeEvent, RuntimeEventType


def _recorder(tmp_path: Path, guard_enabled: bool) -> LiveRecorder:
    meta = LiveRecorderSessionMeta(
        session_ts=1_700_000_000,
        endpoints={"venue": "offline://pmxt-txline"},
        tool_version="pmxt-tape-test",
        config_hash=StrategyConfig(guard_enabled=guard_enabled).config_hash(),
        source_provenance={"venue": "offline"},
        fixture_ids=(pmxt_tape.FIXTURE_ID,),
    )
    return LiveRecorder(tmp_path, meta)


def _fold(tmp_path: Path, guard_enabled: bool):
    events = pmxt_tape.build_tape_events()
    recorder = _recorder(tmp_path, guard_enabled)
    run = run_cadence(recorder, events, guard_enabled=guard_enabled)
    recorder.finalize(ended_ts=99_999_999_999)
    recorder.close()
    return events, run


# --- authentication: the honest-builder events pass run_cadence field-for-field ---------------


def test_tape_events_pass_run_cadence_authentication(tmp_path: Path) -> None:
    events, run = _fold(tmp_path, guard_enabled=False)
    assert any(isinstance(e, FvArrival) for e in events)
    ticks = [e for e in events if isinstance(e, ObservationTick)]
    assert ticks, "expected book ObservationTicks"
    # run_cadence RAISES on any forged assembler-owned field; reaching here == authentication proof.
    assert len(run.observations) == len(ticks)
    assert all(obs.guard_fv is None for obs in run.observations)  # guard-off baseline byte-identity


def test_guard_on_folds_and_authenticates(tmp_path: Path) -> None:
    # M1: guard_enabled=TRUE end-to-end through the SAME cadence engine — the FV leg is projected and
    # authenticated (a forged guard leg would fail closed in run_cadence).
    _events, run = _fold(tmp_path, guard_enabled=True)
    assert run.observations, "guard-on cadence must mint observations"
    assert any(obs.guard_fv is not None for obs in run.observations), (
        "M1: with the guard ON at least one observation must carry a projected TxLINE FV leg"
    )


# --- M2: correct FV time semantics ------------------------------------------------------------


def test_fv_time_semantics_are_correct() -> None:
    # source_ts is SECONDS (matches the normalizer's Ts//1000); recv_ts is the FV's OWN source Ts in
    # ms (NOT book_recv_ts-1); source_ts is never a millisecond value assigned to a seconds field.
    events = pmxt_tape.build_tape_events()
    fvs = [e for e in events if isinstance(e, FvArrival)]
    assert fvs
    for fv in fvs:
        # recv_ts (ms) // 1000 == source_ts (seconds): recv_ts is the same real source clock, ms-scaled.
        assert fv.recv_ts // 1000 == fv.source_ts, (
            f"recv_ts(ms)={fv.recv_ts} must be the ms form of source_ts(s)={fv.source_ts}"
        )
        # source_ts is a plausible unix-SECONDS timestamp (10 digits), never a millisecond value.
        assert 1_000_000_000 <= fv.source_ts <= 9_999_999_999
    # the FvArrival recv_ts is decoupled from any book tick (no recv_ts = book_recv_ts - 1 coupling).
    ticks = [e for e in events if isinstance(e, ObservationTick)]
    book_recv = {t.recv_ts for t in ticks}
    assert not any((fv.recv_ts + 1) in book_recv for fv in fvs), (
        "M2: FvArrival.recv_ts must not be book_recv_ts-1 (the SX-hybrid defect)"
    )


# --- M3: FV dedup — only emit a new arrival when the recorded FV value changes -----------------


def test_fv_dedup_collapses_repeated_snapshots() -> None:
    # NQ-3: ISOLATE dedup from the unrelated "side unpriced on this tick -> skip" path. Rebuild the
    # PRICED part1 fair-value series (bps not None) WITHOUT dedup, mirroring _load_fv_arrivals minus
    # the M3 collapse, so `priced` ALREADY excludes the None-skips. The drop we then assert can ONLY be
    # explained by dedup — a bare `len(fvs) < len(states)` would also pass on None-skips alone.
    states = load_pack_marketstates(pmxt_tape.pack_dir(), pmxt_tape.FIXTURE_ID, verify=True)
    priced: list[float] = []
    for state in states:
        market = state.markets.get(pmxt_tape._TXLINE_1X2_FULL_MARKET_KEY)
        if not market:
            continue
        bps = (market.get("stable_prob_bps") or {}).get(pmxt_tape.TXLINE_SIDE)
        if bps is None:
            continue  # unpriced side — the skip path we must NOT count as a dedup collapse
        priced.append(bps / 1e4)
    # consecutive-duplicate PRICED records are exactly what dedup MUST drop.
    collapses = sum(
        1 for prev, cur in zip(priced, priced[1:], strict=False) if round(prev, 6) == round(cur, 6)
    )
    assert collapses >= 1, (
        "test precondition: the committed pack must contain >=1 consecutive-duplicate PRICED "
        "fair value, else this test cannot isolate dedup from the unpriced-skip path"
    )

    events = pmxt_tape.build_tape_events()
    fvs = [e for e in events if isinstance(e, FvArrival)]
    # dedup drops EXACTLY the consecutive-duplicate priced records — arrivals == priced - collapses.
    # (`priced` already excludes unpriced ticks, so this delta is attributable to dedup ALONE.)
    assert len(fvs) == len(priced) - collapses, (
        f"M3: dedup must drop exactly the {collapses} consecutive-duplicate priced snapshot(s): "
        f"{len(fvs)} arrivals vs {len(priced)} priced - {collapses} collapses"
    )
    # every consecutive emitted arrival differs from its predecessor in value (a real change).
    for prev, cur in zip(fvs, fvs[1:], strict=False):
        assert round(prev.value, 6) != round(cur.value, 6), (
            "M3: consecutive FvArrivals must carry distinct fair values"
        )


# --- M4: real depth — level_count_in_band comes from the REAL 10-level ladder ------------------


def test_level_count_is_derived_from_real_ladder(tmp_path: Path) -> None:
    _events, run = _fold(tmp_path, guard_enabled=False)
    counts = {obs.level_count_in_band for obs in run.observations}
    # a fabricated gate-clearing constant would be a single value; a REAL ladder varies AND the sizes
    # are real (not a placeholder). The band count is bounded by 20 (10 bids + 10 asks).
    assert len(counts) >= 2, f"M4: level_count must vary with the real ladder, got {counts}"
    assert all(0 <= c <= 20 for c in counts)
    assert all(obs.bid_size is not None and obs.bid_size > 0 for obs in run.observations)
    assert all(obs.ask_size is not None and obs.ask_size > 0 for obs in run.observations)
    # the real sizes are the genuine (large) Polymarket resting sizes, never a placeholder constant.
    assert len({round(obs.bid_size, 2) for obs in run.observations}) >= 2


# --- provenance: real fixture 18209181 (France v Morocco), NOT synthetic / NOT the SX fixture --


def test_provenance_is_real_fixture_france_morocco() -> None:
    tape = pmxt_tape.build_pmxt_txline_tape()
    assert tape.identity.fixture_id == pmxt_tape.FIXTURE_ID == 18209181
    assert tape.identity.fixture_id not in (1, 18213979)  # not TEAM-A/YES, not the SX hybrid fixture
    assert tape.identity.market_ref != "TEAM-A/YES"
    assert tape.identity.token_id != "TOKEN-YES"
    assert pmxt_tape.HOME_TEAM == "France" and pmxt_tape.AWAY_TEAM == "Morocco"
    for event in tape.events:
        assert event.identity.fixture_id == 18209181


def test_provenance_matches_registry_and_is_in_play() -> None:
    repo_root = Path(pmxt_tape.__file__).resolve().parents[2]
    registry = json.loads(
        (repo_root / "scripts" / "txline_live" / "wc-qf-fixtures.json").read_text()
    )
    entry = next(f for f in registry if int(f["fixture_id"]) == pmxt_tape.FIXTURE_ID)
    assert entry["event_slug"] == pmxt_tape.EVENT_SLUG == "fifwc-fra-mar-2026-07-09"
    assert entry["home_team"] == pmxt_tape.HOME_TEAM == "France"
    assert entry["away_team"] == pmxt_tape.AWAY_TEAM == "Morocco"
    assert int(entry["kickoff_ts"]) == pmxt_tape.KICKOFF_TS == 1783627200
    # every real depth row + FV record in the committed window is strictly post-kickoff (in-play).
    kickoff_ms = pmxt_tape.KICKOFF_TS * 1000
    depth_rows = pmxt_tape.load_depth_slice()
    assert depth_rows and all(int(r["recv_ts_ms"]) > kickoff_ms for r in depth_rows)
    events = pmxt_tape.build_tape_events()
    fvs = [e for e in events if isinstance(e, FvArrival)]
    assert all(fv.recv_ts > kickoff_ms for fv in fvs)


def test_v1_pack_content_hash_verifies_and_is_data_only() -> None:
    # the committed sub-pack is a REAL, content-hash-verified slice; and it is a v1 (data-only) pack —
    # research-grade, NOT an R3-sealed / cryptographically-genuine capture (honesty disclosure).
    pack_dir = pmxt_tape.pack_dir()
    assert verify_content_hash(pack_dir), "committed sub-pack must pass content_hash verification"
    manifest = json.loads((pack_dir / "pack.json").read_text())
    assert int(manifest["pack_version"]) == 1, "v1 = DATA-ONLY hash (not authority-sealed)"


def test_depth_and_fv_are_on_distinct_recorder_clocks() -> None:
    # honesty: the two legs are on DIFFERENT recorder clocks (pmxt recorder ms vs TxLINE source ms).
    # We assert only that both are real unix-ms of the same match window (seconds-scale) — NEVER a
    # sub-2s single-clock lead claim.
    depth_rows = pmxt_tape.load_depth_slice()
    events = pmxt_tape.build_tape_events()
    fvs = [e for e in events if isinstance(e, FvArrival)]
    dmin = min(int(r["recv_ts_ms"]) for r in depth_rows)
    dmax = max(int(r["recv_ts_ms"]) for r in depth_rows)
    fmin = min(fv.recv_ts for fv in fvs)
    fmax = max(fv.recv_ts for fv in fvs)
    # overlapping in-match windows on the two clocks (seconds-scale), not a sub-2s aligned pair.
    assert fmin <= dmax and dmin <= fmax


# --- catalog resolution + content-hash re-verification ----------------------------------------


def test_catalog_resolves_and_hash_reverifies() -> None:
    assert pmxt_tape.TAPE_REF == "pmxt-txline-mm-18209181-v1"
    assert pmxt_tape.TAPE_REF in sf.MM_TAPE_CATALOG
    tape = sf.default_mm_tape_resolver(pmxt_tape.TAPE_REF)
    assert isinstance(tape, sf.MakerReplayTape)
    assert tape.tape_ref == pmxt_tape.TAPE_REF
    assert tape.content_hash == sf.compute_tape_content_hash(tape.events)
    # deterministic rebuild reproduces the identical content hash.
    assert pmxt_tape.build_pmxt_txline_tape().content_hash == tape.content_hash


# --- as-of join: within the SEALED merged sequence the selected FV is no-look-ahead on the DERIVED
# --- recv_ts. This is NOT a strict cross-clock causal-arrival guarantee — the book pmxt-recorder
# --- arrival clock and the FV source-``Ts`` clock are independent with no calibrated offset (see
# --- pmxt_tape "THE JOIN" / honesty boundaries). ----------------------------------------------


def test_join_is_no_look_ahead_on_derived_recv_ts(tmp_path: Path) -> None:
    """The as-of selection is deterministic + no-look-ahead ON THE DERIVED ``recv_ts`` clock.

    This pins the property the construction ACTUALLY supports: within the sealed global
    ``(recv_ts, sequence_no)`` sequence a book tick only ever pairs with an FV whose (derived) arrival
    ``recv_ts`` is at-or-before its own decision clock — never future-dated on that clock, and
    reproducible across a rebuild of the sealed events.

    It deliberately does NOT assert a strict cross-clock causal ordering (book pmxt-recorder-arrival
    vs FV source-``Ts``): those are independent recorder clocks with no calibrated offset, so "the
    TxLINE value physically arrived before the book decision" is NOT a property this tape can prove.
    (The former cross-clock ``fv_source_ts <= book_recv_ts//1000 + 1`` assertion — with its +1 fudge —
    is REMOVED: it implied exactly that unsupported causal ordering. The real guarantee is the
    derived-clock ``fv_recv_ts <= as_of_ts`` check below. Brief Major 2 / NQ-2.)
    """
    _events, run = _fold(tmp_path, guard_enabled=True)
    guarded = [obs for obs in run.observations if obs.guard_fv is not None]
    assert guarded, "guard-on run must project at least one FV leg to check"
    for obs in guarded:
        # THE guarantee: the projected FV's (derived) arrival is at-or-before the book decision clock
        # in the sealed merged sequence — never future-dated on the derived recv_ts.
        assert obs.guard_fv.fv_recv_ts <= obs.as_of_ts
    # deterministic/reproducible: rebuilding the sealed events yields the identical guarded projection
    # (the selection is a pure function of the sealed sequence, not of wall-clock arrival).
    _events2, run2 = _fold(tmp_path / "rebuild", guard_enabled=True)
    rebuilt = [obs.guard_fv.fv_recv_ts for obs in run2.observations if obs.guard_fv is not None]
    assert rebuilt == [obs.guard_fv.fv_recv_ts for obs in guarded]


# --- M1: matched guard-OFF vs guard-ON behavior over the SAME tape (report the difference) -----


def _decide_stream(run, guard_enabled: bool) -> list[str]:
    cfg = StrategyConfig(guard_enabled=guard_enabled)
    state = StrategyState()
    kinds: list[str] = []
    for obs in run.observations:
        decision, state = decide(obs, state, cfg)
        kinds.append(decision.kind)
    return kinds


def test_matched_guard_off_on_behavior(tmp_path: Path) -> None:
    _off_events, off_run = _fold(tmp_path / "off", guard_enabled=False)
    _on_events, on_run = _fold(tmp_path / "on", guard_enabled=True)
    assert len(off_run.observations) == len(on_run.observations)

    off_kinds = _decide_stream(off_run, guard_enabled=False)
    on_kinds = _decide_stream(on_run, guard_enabled=True)

    off_quotes = sum(1 for k in off_kinds if k.startswith("QUOTE"))
    on_quotes = sum(1 for k in on_kinds if k.startswith("QUOTE"))
    # BOTH arms quote against the real book (attempted-leg capable).
    assert off_quotes >= 1 and on_quotes >= 1
    # HONEST REPORT: the guard CHANGES decisions — on this real window the guard turns some quoting
    # frames into abstentions (the TxLINE FV was stale). We assert only the BEHAVIOR difference; no
    # economic claim (no matched markout accounting).
    divergences = sum(1 for a, b in zip(off_kinds, on_kinds, strict=True) if a != b)
    assert divergences >= 1, (
        "M1: the guard must change at least one decision vs the baseline over the same real tape"
    )
    # the guard's net effect here is FEWER quotes (it abstained on stale-FV frames), never MORE.
    assert on_quotes <= off_quotes


def test_guard_on_abstains_on_stale_txline_fv(tmp_path: Path) -> None:
    # M1 mechanism: the observable divergence is the guard abstaining when the real TxLINE FV is stale.
    from veridex.mm_strategy.contracts import StrategyState as _S

    _events, run = _fold(tmp_path, guard_enabled=True)
    cfg = StrategyConfig(guard_enabled=True)
    state = _S()
    reasons: list[str] = []
    for obs in run.observations:
        decision, state = decide(obs, state, cfg)
        reasons.extend(getattr(decision, "reason_codes", ()) or ())
    assert "txline_stale" in reasons, (
        "M1: the guard must (honestly) abstain on at least one stale real TxLINE FV frame"
    )


# --- M5: real Studio guard-ON payload E2E through the PRODUCTION catalog -----------------------


#: THE ONE shared canonical Studio MM deploy payload — the SAME committed fixture the frontend contract
#: test (``apps/web/components/screens/StudioScreen.test.tsx``) pins ``buildDeployPayload(...)`` against.
#: Driving this backend E2E off the SAME file means the real UI click path and the backend it resolves
#: cannot drift (the parked Major-1 defect: the UI emitted a facsimile — ``synthetic-mm-mechanism-v1``
#: + sxbet/1X2 — that no UI actually emits and the production catalog cannot resolve).
_CANONICAL_MM_PAYLOAD_PATH = (
    Path(pmxt_tape.__file__).resolve().parents[2]
    / "contracts"
    / "fixtures"
    / "studio_mm_deploy_payload.json"
)


def _mm_payload(**overrides: Any) -> dict[str, Any]:
    """The canonical Studio MM deploy payload the REAL UI emits (shared fixture), plus test overrides.

    Loaded verbatim from the committed contract fixture — NOT a Python facsimile. It carries the
    PMXT-coherent identity (``market_allowlist[0] == pmxt:18209181:home_win`` on venue ``poly``) and
    the real-data tape key ``pmxt-txline-mm-18209181-v1``, so it resolves through the PRODUCTION catalog.
    """
    payload: dict[str, Any] = json.loads(_CANONICAL_MM_PAYLOAD_PATH.read_text())
    # Sanity: the fixture IS the real UI identity (guards against an accidental fixture edit that would
    # silently decouple this E2E from the shipped click path).
    assert payload["mm"]["tape_ref"] == pmxt_tape.TAPE_REF
    assert payload["market_allowlist"] == [pmxt_tape.TOKEN_ID]
    payload.update(overrides)
    return payload


def _transport(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _drain(app: FastAPI) -> None:
    tasks = list(getattr(app.state, "deploy_background_tasks", set()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _receipt_events(events: list[RuntimeEvent]) -> list[RuntimeEvent]:
    return [e for e in events if e.type == RuntimeEventType.TOOL_CALL and "legs" in e.payload]


@pytest.mark.asyncio
async def test_studio_guard_on_payload_produces_ops_and_attempted_receipt() -> None:
    from veridex.store import InMemoryStore

    events: list[RuntimeEvent] = []
    store = InMemoryStore()
    app = FastAPI()
    # mm_tape_resolver=None -> resolve THROUGH the production catalog; mm_seed_state=None -> the tape
    # SELF-WARMS from the default cold seed. guard_enabled=True in the payload -> the FV actually gates.
    deps = DeployDeps(
        anchor_fn=None,
        mm_tape_resolver=None,
        mm_proposer=OfflineRecordingProposer(),
        mm_seed_state=None,
    )
    register_deploy_routes(
        app,
        store=store,
        settings=Settings(AUTH_MODE="dev"),
        deploy_deps=deps,
        runtime_event_sink=events.append,
    )
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    await _drain(app)

    instance = await store.get_agent_instance(body["instance_id"])
    assert instance.status == DeployStatus.SEALED, instance.last_failure_reason
    assert events, "expected OPS telemetry"
    receipts = _receipt_events(events)
    assert receipts, "M5: replay+dry_run guard-ON over the self-warming tape must produce >= 1 receipt"
    assert any(
        leg.get("attempted") for e in receipts for leg in e.payload.get("legs", [])
    ), "M5: the guard-ON self-warming tape must produce >= 1 ATTEMPTED leg"


def test_unknown_ref_still_fails_closed() -> None:
    with pytest.raises(sf.MMTapeNotFoundError):
        sf.default_mm_tape_resolver("not-a-real-tape-ref")


@pytest.mark.asyncio
async def test_run_guard_ablation_diverges_on_real_pmxt_tape(tmp_path: Path) -> None:
    """Step-1 gate, PRODUCTION path: ``run_guard_ablation`` diverges on the GENUINE 18209181 tape.

    The decide-layer divergence is already pinned by ``test_matched_guard_off_on_behavior``; this closes
    the remaining gap by driving the SAME chain the ``/maker/live-ab`` provider will use — seal a Studio
    quoteguard deploy, reconstruct the session from server-owned state, resolve the REAL tape through the
    production catalog, run the guard OFF/ON ablation, and project it via ``build_live_ab_projection``.
    Behavior-ablation only: no R4-A, no rank/edge/profit claim.
    """
    from veridex.api.maker_router import build_live_ab_projection
    from veridex.mm_strategy.composition import run_guard_ablation
    from veridex.runtime.mm_agent_adapter import RunContext
    from veridex.store import InMemoryStore

    store = InMemoryStore()
    app = FastAPI()
    deps = DeployDeps(
        anchor_fn=None, mm_tape_resolver=None, mm_proposer=OfflineRecordingProposer(), mm_seed_state=None
    )
    register_deploy_routes(
        app, store=store, settings=Settings(AUTH_MODE="dev"), deploy_deps=deps, runtime_event_sink=[].append
    )
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload())
    assert resp.status_code == 200, resp.text
    instance_id = resp.json()["instance_id"]
    await _drain(app)

    instance = await store.get_agent_instance(instance_id)
    assert instance.status == DeployStatus.SEALED, instance.last_failure_reason

    # Reconstruct ONLY from server-owned state (the provider's contract) and resolve the REAL tape.
    ctx = RunContext(
        run_id=instance.run_id,
        session_id="sess-ab",
        runtime_agent_id="ra-ab",
        owner_did=instance.operator_id or "did:privy:dev",
    )
    cfg, tape, mode, _guard = sf.reconstruct_mm_session(
        instance, ctx, tape_resolver=None, proposer=OfflineRecordingProposer(), session_dir=tmp_path
    )
    assert tape.identity.fixture_id == pmxt_tape.FIXTURE_ID  # the genuine 18209181 tape

    result = await run_guard_ablation(cfg, tape, mode=mode, event_sink=[].append)

    # PASS-FOR-FLAGSHIP: the guard flip changes >= 1 substantive decision on the GENUINE tape.
    # (Observed on this real 18209181 window: 8 divergent frames, the first flipping a two-sided
    # quote to a no-quote when the real TxLINE FV is stale — behavior ablation, not a ranked result.)
    assert result.diverges, "guard OFF/ON did NOT diverge on the real pmxt tape"
    assert len(result.divergent_frame_indices) >= 1
    # Both arms folded the SAME evidence (the honest ablation invariant).
    assert len(result.guard_off.decisions) == len(result.guard_on.decisions)
    # The provider's projection reports the divergence honestly (same source of truth).
    proj = build_live_ab_projection(result, instance_id=instance_id)
    assert proj.diverges is True
    assert proj.divergent_frame_indices == list(result.divergent_frame_indices)
