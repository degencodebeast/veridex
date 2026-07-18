"""fu-ii5-demo-tape — the ``txline-mm-18213979-v1`` HYBRID maker replay tape banked into MM_TAPE_CATALOG.

Proves the load-bearing claims (A8 honesty): (1) the builder's events PASS ``run_cadence``
field-for-field authentication; (2) the PRODUCTION catalog resolves ``txline-mm-18213979-v1`` and its
content hash re-verifies (every other key still fails closed); (3) the tape is SELF-WARMING — folded
from a COLD ``StrategyState()`` (NO injected seed) the warm state EMERGES from real rows and the run
produces at least one REAL quote OR an honest abstention; (4) a real ``replay`` + ``dry_run`` deploy
that resolves the tape THROUGH the catalog (no injected resolver) + the DEFAULT cold seed emits >= 1 OPS
event AND >= 1 dry-run receipt with an ATTEMPTED leg; (5) provenance is the real SX fixture 18213979,
NEVER the synthetic TEAM-A/YES ``fixture_id=1`` canned fixture.
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
from veridex.live_recorder.contracts import LiveRecorderSessionMeta
from veridex.live_recorder.recorder import LiveRecorder
from veridex.mm_strategy import demo_tape
from veridex.mm_strategy import session_factory as sf
from veridex.mm_strategy.assembler import FvArrival, ObservationTick, run_cadence
from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import StrategyState
from veridex.mm_strategy.core import decide
from veridex.mm_strategy.offline_proposer import OfflineRecordingProposer
from veridex.runtime.runtime_events import RuntimeEvent, RuntimeEventType

_GUARD_OFF = StrategyConfig(guard_enabled=False, tif="GTC")


def _recorder(tmp_path: Path) -> LiveRecorder:
    meta = LiveRecorderSessionMeta(
        session_ts=1_700_000_000,
        endpoints={"venue": "offline://demo"},
        tool_version="demo-tape-test",
        config_hash=_GUARD_OFF.config_hash(),
        source_provenance={"venue": "offline"},
        fixture_ids=(demo_tape.FIXTURE_ID,),
    )
    return LiveRecorder(tmp_path, meta)


def _fold_cadence(tmp_path: Path):
    events = demo_tape.build_tape_events()
    recorder = _recorder(tmp_path)
    run = run_cadence(recorder, events, guard_enabled=False)
    recorder.finalize(ended_ts=10_000_000)
    recorder.close()
    return events, run


# --- (1) authentication -----------------------------------------------------------------------


def test_tape_events_pass_run_cadence_authentication(tmp_path: Path) -> None:
    events, run = _fold_cadence(tmp_path)
    # a genuine slice contributes an FvArrival + an ObservationTick per row.
    assert any(isinstance(e, FvArrival) for e in events)
    ticks = [e for e in events if isinstance(e, ObservationTick)]
    assert ticks, "expected book ObservationTicks"
    # run_cadence authenticates every built observation field-for-field against the tick's owned facts
    # and raises on any forgery — reaching here without raising IS the authentication proof.
    assert len(run.observations) == len(ticks)
    # guard-off: every observation carries guard_fv=None (baseline byte-identity contract).
    assert all(obs.guard_fv is None for obs in run.observations)


# --- (2) catalog resolution + content-hash re-verification -------------------------------------


def test_catalog_resolves_and_hash_reverifies() -> None:
    # the PRODUCTION catalog (not an injected resolver) banks the honest fixture key.
    assert demo_tape.TAPE_REF == "txline-mm-18213979-v1"
    assert demo_tape.TAPE_REF in sf.MM_TAPE_CATALOG
    tape = sf.default_mm_tape_resolver(demo_tape.TAPE_REF)
    assert isinstance(tape, sf.MakerReplayTape)
    assert tape.tape_ref == demo_tape.TAPE_REF
    # the pinned hash equals a fresh recomputation (what reconstruct_mm_session step 7 enforces).
    assert tape.content_hash == sf.compute_tape_content_hash(tape.events)
    # deterministic: a rebuild reproduces the identical content hash.
    assert demo_tape.build_txline_mm_tape().content_hash == tape.content_hash


def test_unknown_ref_still_fails_closed() -> None:
    with pytest.raises(sf.MMTapeNotFoundError):
        sf.default_mm_tape_resolver("not-a-real-tape-ref")


# --- (3) SELF-WARMING: warm state emerges from real rows, no injected seed ---------------------


def test_tape_self_warms_from_cold_state_and_quotes_or_abstains(tmp_path: Path) -> None:
    _events, run = _fold_cadence(tmp_path)
    # fold from the DEPLOY's DEFAULT cold seed — no injected/hand-authored/test-fixture StrategyState.
    cold = StrategyState()
    assert cold.smoother_mid is None and not cold.spread_ref_samples and not cold.depth_ref_samples

    state = cold
    quoting = 0
    legs = 0
    kinds: dict[str, int] = {}
    for obs in run.observations:
        decision, state = decide(obs, state, _GUARD_OFF)
        kinds[decision.kind] = kinds.get(decision.kind, 0) + 1
        if decision.intent_plan:
            quoting += 1
            legs += len(decision.intent_plan)

    # the warm state EMERGED from folding real rows (smoother seeded + references past ref_min_samples).
    assert state.smoother_mid is not None
    assert len(state.spread_ref_samples) >= _GUARD_OFF.ref_min_samples
    assert len(state.depth_ref_samples) >= _GUARD_OFF.ref_min_samples
    # acceptance: at least one REAL quote OR a meaningful abstention (both are honest outcomes).
    assert quoting >= 1 or any(k in kinds for k in ("NO_QUOTE", "HOLD"))
    # this real-data window in fact rests quotes once warm (documents the observed outcome).
    assert quoting >= 1 and legs >= 1


# --- (4) provenance: real SX fixture, NOT the synthetic TEAM-A/YES fixture ---------------------


def test_provenance_is_real_capture_not_synthetic_fixture() -> None:
    tape = demo_tape.build_txline_mm_tape()
    assert tape.identity.fixture_id == demo_tape.FIXTURE_ID == 18213979
    assert tape.identity.fixture_id != 1  # NOT the TEAM-A/YES canned fixture
    assert tape.identity.market_ref != "TEAM-A/YES"
    assert tape.identity.token_id != "TOKEN-YES"
    # the key names the fixture, NOT a round/stage (registry has no stage field -> no "qf"/"world cup").
    assert "qf" not in demo_tape.TAPE_REF and "world" not in demo_tape.TAPE_REF.lower()
    # every event carries the real fixture identity.
    for event in tape.events:
        assert event.identity.fixture_id == 18213979
    # the committed slice rows are the real fixture's recorded rows.
    rows = demo_tape.load_capture_slice()
    assert rows, "committed slice must be non-empty"
    assert all(int(r["fixture_id"]) == 18213979 for r in rows)


def test_provenance_matches_fixture_registry_and_is_in_play() -> None:
    # cross-check the tape's provenance constants against the REPO fixture registry (the substantiation
    # source) — teams + competition ARE verifiable there; the "in-play" claim is verifiable from recv_ts.
    repo_root = Path(demo_tape.__file__).resolve().parents[2]
    registry = json.loads(
        (repo_root / "scripts" / "txline_live" / "wc-qf-fixtures.json").read_text()
    )
    entry = next(f for f in registry if int(f["fixture_id"]) == demo_tape.FIXTURE_ID)
    assert entry["event_slug"] == demo_tape.EVENT_SLUG == "fifwc-nor-eng-2026-07-11"
    assert entry["home_team"] == demo_tape.HOME_TEAM == "Norway"
    assert entry["away_team"] == demo_tape.AWAY_TEAM == "England"
    assert int(entry["kickoff_ts"]) == demo_tape.KICKOFF_TS == 1783803600

    # IN-PLAY: every captured row's recv_ts (ms) is strictly AFTER kickoff (s -> ms) — ~51' in-play.
    rows = demo_tape.load_capture_slice()
    kickoff_ms = demo_tape.KICKOFF_TS * 1000
    assert all(int(r["recv_ts"]) > kickoff_ms for r in rows), "slice must be post-kickoff (in-play)"
    minutes_in = (min(int(r["recv_ts"]) for r in rows) - kickoff_ms) / 60_000
    assert 40 < minutes_in < 60  # first-half in-play window (~51'), not pre-match


# --- (5, full pipeline) deploy through the catalog -> OPS + dry-run receipt with attempted leg -


def _mm_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "template_id": "quoteguard-mm-template",
        "agent_id": "studio-mm-agent",
        "strategy": "quoteguard-mm",
        "source_mode": "replay",
        "execution_mode": "dry_run",
        "market_allowlist": [demo_tape.TOKEN_ID],
        "venue_allowlist": ["sx"],
        "min_edge_bps": 10,
        "max_stake": 5.0,
        "mm": {"tape_ref": demo_tape.TAPE_REF, "guard_enabled": False},
    }
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
async def test_deploy_through_catalog_produces_ops_and_attempted_receipt() -> None:
    from veridex.store import InMemoryStore

    events: list[RuntimeEvent] = []
    store = InMemoryStore()
    app = FastAPI()
    # mm_tape_resolver=None -> resolves THROUGH the production catalog; mm_seed_state=None -> the tape
    # SELF-WARMS from the default cold seed (the demo's real, no-injected-seed path).
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
    assert receipts, "replay+dry_run over the self-warming tape must produce >= 1 dry-run receipt"
    assert any(
        leg.get("attempted") for e in receipts for leg in e.payload.get("legs", [])
    ), "the self-warming tape must produce >= 1 ATTEMPTED leg once warm"
