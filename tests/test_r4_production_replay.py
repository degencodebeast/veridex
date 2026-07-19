"""R-4 (Option B) — PRODUCTION replay loads the SELECTED verified pack via an atomic-snapshot resolver.

Competition-start and deploy replay source their tape from a REAL, hash-verified ReplayPack through the
SAME ``load_pack_marketstates`` normalizer live replay uses — resolved SERVER-side from the R-2 catalog
under ONE atomic snapshot. The synthetic ``build_demo_ticks`` fixture stays a CI/test artifact and is
NO LONGER a production source (there is no silent fallback to it). These tests pin the Option B contract:

* (a) one catalogued pack + UNNAMED request -> resolves that pack, FREEZES + persists the identity,
      surfaces it, and ``build_demo_ticks`` is never called;
* (b) MULTIPLE packs + UNNAMED -> fail closed ``pack_id_required`` (never guess);
* (c) NAMED unknown/unverified pack -> fail closed ``unknown_pack`` (competition + deploy), no demo tape;
* (d) resolve -> R-0b promotes a 2nd pack -> retry REUSES the frozen binding (not re-selected);
* (e) explicit ``fixture_id=0`` is PRESENCE-AWARE (validated, not aliased to "omitted");
* (f) ``build_demo_ticks`` is unchanged for CI/test callers (and ``/demo/run``);
* (g) ``content_hash`` is SERVER-derived — a client can never supply the tape identity hash.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport

from veridex.api.demo_fixtures import DEMO_MARKET_KEY, build_demo_ticks
from veridex.api.router import create_app
from veridex.competition.models import (
    Competition,
    CompetitionConfig,
    CompetitionStatus,
    ReplayBinding,
)
from veridex.deploy.preflight import DeployConfig
from veridex.ingest.replay_catalog import (
    ReplayResolutionError,
    ResolvedReplaySource,
    build_catalog,
    load_resolved_marketstates,
    resolve_replay_source,
)
from veridex.ingest.replay_pack import (
    _compute_content_hash,
    load_pack_marketstates,
    verify_content_hash,
)
from veridex.store import InMemoryStore

# --- the R-1 banked curated seed pack (the single production pack; catalogs as one entry) --------
SEED = Path(__file__).resolve().parents[1] / "scripts" / "fixtures" / "demo_pack_real"
SEED_PACK_ID = SEED.name  # the catalog derives pack_id from the leaf dir name
_MANIFEST = json.loads((SEED / "pack.json").read_text())
SEED_HASH = _MANIFEST["content_hash"]
SEED_MIN_FIXTURE = min(int(f["fixture_id"]) for f in _MANIFEST["fixtures"])
SEED_COUNT = len(load_pack_marketstates(SEED, SEED_MIN_FIXTURE, verify=True))

_CONFIG: dict[str, Any] = {
    "competition_type": "replay_arena",
    "source_mode": "replay",
    "execution_mode": "paper",
    "market_scope": "WC:TEST",
    "roster_size": 2,
}
_A = {"agent_id": "agent-alpha", "owner": "team-a", "strategy": "baseline", "model": None, "proof_mode": "reproducible"}
_B = {"agent_id": "agent-beta", "owner": "team-b", "strategy": "baseline", "model": None, "proof_mode": "reproducible"}

_REPLAY_STUDIO: dict[str, Any] = {
    "template_id": "sharp-momentum-v2",
    "agent_id": "studio-agent",
    "strategy": "momentum-sharp",
    "source_mode": "replay",
    "execution_mode": "paper",
    "market_allowlist": [],
    "venue_allowlist": ["fake"],
    "window_id": "w1",
    "fixture_id": 1,
    "end_rule": "pre_match",
}


def _boom() -> Any:
    raise AssertionError("build_demo_ticks must not be called on the production replay path (R-4)")


def _seed_catalog() -> Any:
    """A 1-entry R-2 catalog over the curated seed pack (pack_id == 'demo_pack_real')."""
    return build_catalog(str(SEED))


def _multi_catalog(tmp: Path) -> Any:
    """A 2-entry R-2 catalog (two verified packs) — so an UNNAMED request is ambiguous."""
    root = tmp / "packs"
    root.mkdir()
    shutil.copytree(SEED, root / "packA")
    shutil.copytree(SEED, root / "packB")
    return build_catalog(str(root))


def _client(catalog: Any, store: InMemoryStore | None = None) -> TestClient:
    return TestClient(create_app(store=store or InMemoryStore(), replay_catalog=catalog))


def _async_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _register_roster(client: TestClient, comp_id: str) -> None:
    client.post(f"/competitions/{comp_id}/agents", json=_A)
    client.post(f"/competitions/{comp_id}/agents", json=_B)


def _market_ticks(client: TestClient, comp_id: str) -> list[dict[str, Any]]:
    events = client.get(f"/competitions/{comp_id}/events?since_seq=0").json()
    return [e for e in events if e["event_type"] == "market_tick"]


# ---------------------------------------------------------------------------
# (a) one catalogued pack + UNNAMED -> resolves + freezes + persists + not build_demo_ticks
# ---------------------------------------------------------------------------


def test_a_one_pack_unnamed_resolves_and_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("veridex.api.router.build_demo_ticks", _boom)  # any call would 500 the start
    client = _client(_seed_catalog())
    comp_id = client.post("/competitions", json=_CONFIG).json()["competition_id"]  # UNNAMED
    _register_roster(client, comp_id)

    start = client.post(f"/competitions/{comp_id}/start")
    assert start.status_code == 200, start.text
    binding = start.json()["replay_binding"]
    # the auto-resolved identity is surfaced on the response (an unnamed request is NOT unidentified).
    assert binding == {"pack_id": SEED_PACK_ID, "fixture_id": SEED_MIN_FIXTURE, "content_hash": SEED_HASH}

    # persisted + observable via GET /competitions/{id}.
    state = client.get(f"/competitions/{comp_id}").json()
    assert state["replay_binding"] == binding

    # the served tape IS the seed fixture (one MARKET_TICK per seed MarketState — never the 2-tick demo).
    ticks = _market_ticks(client, comp_id)
    assert len(ticks) == SEED_COUNT
    assert SEED_COUNT > 2


# ---------------------------------------------------------------------------
# (b) MULTIPLE packs + UNNAMED -> fail closed pack_id_required
# ---------------------------------------------------------------------------


def test_b_multiple_packs_unnamed_requires_pack_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("veridex.api.router.build_demo_ticks", _boom)  # no silent demo fallback
    client = _client(_multi_catalog(tmp_path))
    comp_id = client.post("/competitions", json=_CONFIG).json()["competition_id"]  # UNNAMED, 2 packs
    _register_roster(client, comp_id)

    start = client.post(f"/competitions/{comp_id}/start")
    assert start.status_code == 400, start.text
    assert start.json()["detail"]["reason"] == "pack_id_required"


# ---------------------------------------------------------------------------
# (c) NAMED unknown pack -> fail closed unknown_pack, no build_demo_ticks (competition + deploy)
# ---------------------------------------------------------------------------


def test_c_named_unknown_pack_fails_closed_competition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("veridex.api.router.build_demo_ticks", _boom)
    client = _client(_seed_catalog())
    # NAMED at create -> freeze-at-admission resolves it -> unknown -> 400 (no competition is created).
    resp = client.post("/competitions", json={**_CONFIG, "pack_id": "does-not-exist"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["reason"] == "unknown_pack"


async def test_c_named_unknown_pack_fails_closed_deploy() -> None:
    app = create_app(store=InMemoryStore(), replay_catalog=_seed_catalog())
    async with _async_client(app) as client:
        resp = await client.post("/agents/deploy", json={**_REPLAY_STUDIO, "replay_pack_id": "does-not-exist"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["reason"] == "unknown_pack"
    assert not getattr(app.state, "deploy_background_tasks", set())  # fail closed: no run launched


# ---------------------------------------------------------------------------
# (d) resolve -> R-0b promotes a 2nd pack -> retry REUSES the frozen binding (not re-selected)
# ---------------------------------------------------------------------------


async def test_d_retry_reuses_frozen_binding_across_r0b_promotion(tmp_path: Path) -> None:
    capture = tmp_path / "cap"
    capture.mkdir()
    catalog = build_catalog(str(SEED), capture_root=str(capture))
    app = create_app(store=InMemoryStore(), replay_catalog=catalog)
    cfg = {**_REPLAY_STUDIO, "replay_pack_id": SEED_PACK_ID}  # bound to the seed pack
    async with _async_client(app) as client:
        first = await client.post("/agents/deploy", json=cfg, headers={"Idempotency-Key": "k-r4d"})
        assert first.status_code == 200, first.text
        b1 = first.json()["replay_binding"]
        assert b1 == {"pack_id": SEED_PACK_ID, "fixture_id": SEED_MIN_FIXTURE, "content_hash": SEED_HASH}

        # R-0b promotes a SECOND verified pack into the live catalog AFTER the deploy froze its identity.
        packB = capture / "packB"
        shutil.copytree(SEED, packB)
        catalog.register_pack(packB)
        assert len(catalog) == 2  # the promotion is live

        # Retry with the SAME idempotency key reconciles to the SAME instance and REUSES the frozen
        # binding verbatim — the tape identity is NOT re-selected against the now-2-pack catalog.
        retry = await client.post("/agents/deploy", json=cfg, headers={"Idempotency-Key": "k-r4d"})
        assert retry.status_code == 200, retry.text
        assert retry.json()["instance_id"] == first.json()["instance_id"]
        assert retry.json()["replay_binding"] == b1

        for task in list(getattr(app.state, "deploy_background_tasks", set())):
            task.cancel()


# ---------------------------------------------------------------------------
# (e) explicit fixture_id=0 is presence-aware — validated, NOT aliased to "omitted"
# ---------------------------------------------------------------------------


async def test_e_explicit_fixture_zero_is_presence_aware() -> None:
    # The seed has NO fixture 0. An explicit ``replay_fixture_id=0`` must fail closed ``unknown_fixture``
    # — a ``0 or None`` alias would treat it as omitted and SILENTLY pick the pack's min fixture instead.
    app = create_app(store=InMemoryStore(), replay_catalog=_seed_catalog())
    async with _async_client(app) as client:
        resp = await client.post(
            "/agents/deploy",
            json={**_REPLAY_STUDIO, "replay_pack_id": SEED_PACK_ID, "replay_fixture_id": 0},
        )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["reason"] == "unknown_fixture"


def test_e_resolver_honors_fixture_zero_when_catalogued() -> None:
    # Unit-level: fixture 0 is a VALID id. Presence-aware selection rejects an explicit 0 (unknown_fixture)
    # rather than silently substituting the pack's min — and ``None`` selects the deterministic lowest id.
    with pytest.raises(ReplayResolutionError) as exc:
        resolve_replay_source(_seed_catalog(), pack_id=SEED_PACK_ID, fixture_id=0)
    assert exc.value.reason == "unknown_fixture"
    resolved = resolve_replay_source(_seed_catalog(), pack_id=SEED_PACK_ID, fixture_id=None)
    assert resolved.fixture_id == SEED_MIN_FIXTURE


# ---------------------------------------------------------------------------
# (f) build_demo_ticks is unchanged for CI/test callers (and /demo/run)
# ---------------------------------------------------------------------------


def test_f_build_demo_ticks_still_available_for_ci() -> None:
    ticks = build_demo_ticks()
    assert len(ticks) == 2
    assert ticks[0].fixture_id == 17588404
    assert DEMO_MARKET_KEY in ticks[0].markets


def test_f_demo_run_endpoint_still_uses_demo_fixture() -> None:
    client = _client(_seed_catalog())
    resp = client.post("/demo/run")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["leaderboard"]) >= 2


# ---------------------------------------------------------------------------
# (g) content_hash is SERVER-derived — a client can never supply the tape identity hash
# ---------------------------------------------------------------------------


def test_g_content_hash_is_server_derived_competition() -> None:
    client = _client(_seed_catalog())
    # A client tries to smuggle a bogus content_hash into the create body. CompetitionConfig has NO
    # content_hash field, so it is dropped; the frozen binding's hash is the CATALOG's verified hash.
    resp = client.post("/competitions", json={**_CONFIG, "pack_id": SEED_PACK_ID, "content_hash": "deadbeef"})
    assert resp.status_code == 200, resp.text
    comp_id = resp.json()["competition_id"]
    binding = client.get(f"/competitions/{comp_id}").json()["replay_binding"]
    assert binding["content_hash"] == SEED_HASH
    assert binding["content_hash"] != "deadbeef"


async def test_g_content_hash_is_server_derived_deploy() -> None:
    app = create_app(store=InMemoryStore(), replay_catalog=_seed_catalog())
    async with _async_client(app) as client:
        resp = await client.post("/agents/deploy", json={**_REPLAY_STUDIO, "replay_pack_id": SEED_PACK_ID})
        assert resp.status_code == 200, resp.text
        assert resp.json()["replay_binding"]["content_hash"] == SEED_HASH
        for task in list(getattr(app.state, "deploy_background_tasks", set())):
            task.cancel()


# ---------------------------------------------------------------------------
# (h) the tape identity is bound to the SEALED run: pack SELECTION ∈ config_hash, and content_hash
#     drift is FAIL-CLOSED at load (a run can NEVER seal over bytes different from the frozen hash).
# ---------------------------------------------------------------------------


def test_h_pack_selection_is_bound_into_deploy_config_hash() -> None:
    # The resolved pack SELECTION (replay_pack_id + replay_fixture_id) rides in DeployConfig.config_hash()
    # — reconstruct_mm_session recomputes this from submitted_config and re-verifies it (fail-closed). So
    # the sealed run identity commits to WHICH pack/fixture was chosen: you cannot swap the selected pack
    # without changing the pinned config_hash (and thus the run's identity).
    base = {**_REPLAY_STUDIO, "strategy": "momentum-sharp"}
    h_none = DeployConfig(**base).config_hash()
    h_pack_a = DeployConfig(**{**base, "replay_pack_id": "pack-a"}).config_hash()
    h_pack_b = DeployConfig(**{**base, "replay_pack_id": "pack-b"}).config_hash()
    h_fix = DeployConfig(**{**base, "replay_pack_id": "pack-a", "replay_fixture_id": 7}).config_hash()
    assert len({h_none, h_pack_a, h_pack_b, h_fix}) == 4  # every selection change changes config_hash


def test_h_content_hash_drift_is_fail_closed_at_load() -> None:
    # The FROZEN content_hash is re-checked against the live catalog at LOAD. If an R-0b re-publish drifts
    # the pack's bytes (same pack_id, new content_hash), the frozen identity NO LONGER matches and the
    # load is REFUSED — so a run can never seal over bytes whose content_hash differs from the frozen one
    # (this is what makes the durable binding transitively bind the replayed bytes to the sealed run).
    catalog = _seed_catalog()
    stale = ResolvedReplaySource(
        pack_id=SEED_PACK_ID,
        fixture_id=SEED_MIN_FIXTURE,
        content_hash="stale-drifted-hash",  # a hash that no longer matches the catalogued bytes
        provenance="",
        is_genuine=False,
    )
    with pytest.raises(ReplayResolutionError) as exc:
        load_resolved_marketstates(catalog, stale)
    assert exc.value.reason == "content_hash_drift"
    # the correct frozen hash still loads (control): drift-detection is not over-broad.
    good = resolve_replay_source(catalog, pack_id=SEED_PACK_ID, fixture_id=None)
    assert load_resolved_marketstates(catalog, good)


# ---------------------------------------------------------------------------
# R-4 spec-fold residuals (MINOR 1/2/3)
# ---------------------------------------------------------------------------


async def test_fold1_unbound_binding_persist_is_cas_only_if_unbound() -> None:
    # MINOR 1 (honesty): the unbound-start persist must be an ONLY-IF-UNBOUND compare-and-set.
    # Two concurrent starts of an unbound DRAFT can race an R-0b re-register: the WINNER (claims the
    # run, replays its tape) freezes its binding first; the LOSER (409 at the claim) resolves a
    # DIFFERENT hash and, with the OLD unconditional overwrite, clobbers the winner's binding AFTER
    # the winning run already replayed — so GET reports a hash the run never replayed. CAS fixes it:
    # once bound, a later persist is a no-op and the winner's binding stands.
    store = InMemoryStore()
    comp = Competition(
        competition_id="comp-fold1",
        config=CompetitionConfig.model_validate(_CONFIG),
        status=CompetitionStatus.DRAFT,
        entries=[],
        run_id=None,
    )
    await store.create_competition(comp)
    winner = ReplayBinding(pack_id=SEED_PACK_ID, fixture_id=SEED_MIN_FIXTURE, content_hash="hash-winner")
    loser = ReplayBinding(pack_id=SEED_PACK_ID, fixture_id=SEED_MIN_FIXTURE, content_hash="hash-loser")

    await store.update_competition_replay_binding("comp-fold1", winner)  # the winning run freezes first
    await store.update_competition_replay_binding("comp-fold1", loser)  # the loser must NOT clobber it

    persisted = await store.get_competition("comp-fold1")
    assert persisted.replay_binding is not None
    # GET reports exactly the binding the winning run replayed — never the loser's late overwrite.
    assert persisted.replay_binding.content_hash == "hash-winner"


async def test_fold2_unnamed_replay_retry_reconciles_before_reresolve(tmp_path: Path) -> None:
    # MINOR 2 (availability): the idempotent-existing instance must be reconciled BEFORE the replay
    # source is re-resolved. A successful UNNAMED single-pack replay deploy freezes its tape identity;
    # if an R-0b promotion later makes the catalog multi-pack, a retry that re-resolves FIRST would
    # 400 ``pack_id_required`` instead of returning the existing instance. The frozen binding is
    # authoritative and reused verbatim — the retry stays available and the tape never changes.
    capture = tmp_path / "cap"
    capture.mkdir()
    catalog = build_catalog(str(SEED), capture_root=str(capture))
    app = create_app(store=InMemoryStore(), replay_catalog=catalog)
    cfg = {**_REPLAY_STUDIO}  # UNNAMED: no replay_pack_id -> single-pack auto-resolution
    cfg.pop("replay_pack_id", None)
    async with _async_client(app) as client:
        first = await client.post("/agents/deploy", json=cfg, headers={"Idempotency-Key": "k-fold2"})
        assert first.status_code == 200, first.text
        b1 = first.json()["replay_binding"]
        assert b1 == {"pack_id": SEED_PACK_ID, "fixture_id": SEED_MIN_FIXTURE, "content_hash": SEED_HASH}

        # R-0b promotes a SECOND verified pack -> an UNNAMED re-resolve is now ambiguous.
        packB = capture / "packB"
        shutil.copytree(SEED, packB)
        catalog.register_pack(packB)
        assert len(catalog) == 2

        # Retry with the SAME key: reconcile the existing instance BEFORE re-resolving -> 200, not 400.
        retry = await client.post("/agents/deploy", json=cfg, headers={"Idempotency-Key": "k-fold2"})
        assert retry.status_code == 200, retry.text
        assert retry.json()["instance_id"] == first.json()["instance_id"]
        assert retry.json()["replay_binding"] == b1  # frozen tape identity unchanged

        for task in list(getattr(app.state, "deploy_background_tasks", set())):
            task.cancel()


def test_fold3_recomputed_disk_hash_must_match_frozen_binding(tmp_path: Path) -> None:
    # MINOR 3 (direct byte guarantee): the load must DIRECTLY compare the RECOMPUTED on-disk content
    # hash to the frozen ``resolved.content_hash`` — dropping the pack_dir-immutability assumption.
    # Construct a CONSISTENT on-disk tamper (data + manifest both rewritten so the pack is self-
    # consistent at a NEW hash) while the in-memory catalog entry still carries the ORIGINAL hash.
    # Today: the entry-vs-frozen check matches (metadata) and verify=True passes (disk self-consistent),
    # so the load would serve bytes whose recomputed hash differs from the frozen identity. Fail-closed.
    packroot = tmp_path / "packs"
    packroot.mkdir()
    packx = packroot / "packx"
    shutil.copytree(SEED, packx)
    catalog = build_catalog(str(packroot))  # curated seeds are catalogued IN PLACE (no owned copy)
    entry = catalog.snapshot()["packx"]
    assert entry.content_hash == SEED_HASH  # same bytes as the seed at admission
    pack_dir = Path(entry.pack_dir)

    manifest = json.loads((pack_dir / "pack.json").read_text())
    fixtures = manifest["fixtures"]
    # Tamper a fixture the loader will NOT read (keeps the loaded fixture parseable), then rewrite the
    # manifest hash so the pack stays self-consistent (verify=True passes) at a NEW recomputed hash.
    victim = pack_dir / fixtures[-1]["records"]
    victim.write_bytes(victim.read_bytes() + b'{"tampered": true}\n')
    new_hash = _compute_content_hash(
        pack_dir,
        fixtures,
        pack_version=int(manifest.get("pack_version", 1)),
        capture=manifest.get("capture", {}),
    )
    assert new_hash != SEED_HASH
    manifest["content_hash"] = new_hash
    (pack_dir / "pack.json").write_text(json.dumps(manifest))
    assert verify_content_hash(pack_dir)  # disk is self-consistent at the NEW hash

    # The frozen identity (and the still-cached catalog entry) carry the ORIGINAL hash.
    stale = ResolvedReplaySource(
        pack_id="packx",
        fixture_id=SEED_MIN_FIXTURE,
        content_hash=SEED_HASH,
        provenance="",
        is_genuine=False,
    )
    with pytest.raises(ReplayResolutionError) as exc:
        load_resolved_marketstates(catalog, stale)
    assert exc.value.reason == "content_hash_drift"


def test_fold_start_binding_aligns_to_persisted_authority_under_freeze_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MINOR 1 mirror (honesty): freeze-order vs claim-order residual race. The unbound-start persist is
    # an only-if-unbound CAS, so a start can LOSE the freeze (a concurrent unbound start froze first) yet
    # still WIN the run claim. If the caller keeps its OWN local ``resolved`` for the tape LOAD and the
    # response after a CAS no-op, the run replays one identity while GET reports the persisted winner's —
    # the exact mirror of the clobber the CAS closed. The fix re-reads the AUTHORITATIVE persisted binding
    # and replays/reports THAT, so the response identity, the replayed tape, and GET all agree.
    fixtures = sorted(int(f["fixture_id"]) for f in _MANIFEST["fixtures"])
    winner_fixture = fixtures[-1]  # a DIFFERENT valid fixture than the unnamed (min) auto-resolution
    assert winner_fixture != SEED_MIN_FIXTURE

    store = InMemoryStore()
    client = _client(_seed_catalog(), store=store)
    comp_id = client.post("/competitions", json=_CONFIG).json()["competition_id"]  # UNNAMED, unbound DRAFT
    _register_roster(client, comp_id)

    import veridex.api.router as router_mod

    real_resolve = router_mod.resolve_replay_source

    def _resolve_then_concurrent_freeze(catalog: Any, *, pack_id: Any, fixture_id: Any) -> Any:
        # Simulate a CONCURRENT unbound start that WON the freeze between this handler's read and its CAS:
        # commit a DIFFERENT (winner) binding straight into the store BEFORE this start's CAS runs, so the
        # CAS is a no-op and this start becomes the persist-LOSER (which nonetheless wins the run claim).
        resolved = real_resolve(catalog, pack_id=pack_id, fixture_id=fixture_id)
        store._competitions[comp_id].replay_binding = ReplayBinding(
            pack_id=SEED_PACK_ID, fixture_id=winner_fixture, content_hash=SEED_HASH
        )
        return resolved

    monkeypatch.setattr(router_mod, "resolve_replay_source", _resolve_then_concurrent_freeze)

    start = client.post(f"/competitions/{comp_id}/start")
    assert start.status_code == 200, start.text

    persisted = client.get(f"/competitions/{comp_id}").json()["replay_binding"]
    assert persisted["fixture_id"] == winner_fixture  # the concurrent winner owns the authoritative identity
    # Honesty invariant: GET never reports an identity the run did not replay. The response identity (what
    # the run replayed) MUST equal the persisted authority — not this start's local (min) resolution.
    assert start.json()["replay_binding"] == persisted


def test_fold_m2_hash_valid_fixture_file_swap_fails_closed(tmp_path: Path) -> None:
    # MAJOR 2: content_hash covers sorted (filename, bytes) + the authority block, but NOT the
    # fixture->file mapping. Swapping two fixtures' ``fixture_id`` values in pack.json (records files
    # unchanged) keeps ``verify_content_hash`` True while pointing the frozen fixture at ANOTHER
    # fixture's records. The load must FAIL CLOSED (fixture_mapping_mismatch) rather than replay a
    # different fixture than the sealed identity.
    packroot = tmp_path / "packs"
    packroot.mkdir()
    packx = packroot / "packx"
    shutil.copytree(SEED, packx)
    catalog = build_catalog(str(packroot))
    entry = catalog.snapshot()["packx"]
    pack_dir = Path(entry.pack_dir)

    fixtures = sorted(int(f["fixture_id"]) for f in _MANIFEST["fixtures"])
    frozen, other = fixtures[0], fixtures[1]
    resolved = ResolvedReplaySource(
        pack_id="packx", fixture_id=frozen, content_hash=entry.content_hash, provenance="", is_genuine=False
    )

    # Swap the two entries' fixture_id VALUES (records files untouched). pack.json is not part of the
    # content hash, so the swap is hash-valid but re-points frozen `frozen` at `other`'s records file.
    manifest = json.loads((pack_dir / "pack.json").read_text())
    by_fid = {int(f["fixture_id"]): f for f in manifest["fixtures"]}
    by_fid[frozen]["fixture_id"], by_fid[other]["fixture_id"] = other, frozen
    (pack_dir / "pack.json").write_text(json.dumps(manifest))
    assert verify_content_hash(pack_dir)  # the swap leaves content_hash valid (mapping is unhashed)

    with pytest.raises(ReplayResolutionError) as exc:
        load_resolved_marketstates(catalog, resolved)
    assert exc.value.reason == "fixture_mapping_mismatch"


def test_fold_m3_toctou_swap_between_parse_and_hash_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MAJOR 3: the post-load hash check must not hash a SECOND, independent disk snapshot. Model the
    # TOCTOU: the admitted pack on disk is a self-consistent tampered variant (a record relabelled to
    # fixture 999999, manifest re-hashed) while the loader PARSES it, then it is restored to the ORIGINAL
    # bytes before any separate hash read — so a load that hashes a second read would accept the frozen
    # hash while returning the tampered tape. A single in-memory snapshot (hash == parsed bytes) rejects it.
    packroot = tmp_path / "packs"
    packroot.mkdir()
    packx = packroot / "packx"
    shutil.copytree(SEED, packx)
    catalog = build_catalog(str(packroot))  # entry.content_hash captured over the ORIGINAL bytes
    entry = catalog.snapshot()["packx"]
    pack_dir = Path(entry.pack_dir)
    h_orig = entry.content_hash
    resolved = ResolvedReplaySource(
        pack_id="packx", fixture_id=SEED_MIN_FIXTURE, content_hash=h_orig, provenance="", is_genuine=False
    )

    manifest = json.loads((pack_dir / "pack.json").read_text())
    min_entry = next(f for f in manifest["fixtures"] if int(f["fixture_id"]) == SEED_MIN_FIXTURE)
    records_path = pack_dir / min_entry["records"]
    pack_json_path = pack_dir / "pack.json"
    orig_records = records_path.read_bytes()
    orig_pack_json = pack_json_path.read_bytes()

    # Write a SELF-CONSISTENT tampered variant to disk (first record -> FixtureId 999999, manifest rehashed).
    lines = orig_records.decode("utf-8").splitlines()
    first = json.loads(lines[0])
    first["FixtureId"] = 999999
    lines[0] = json.dumps(first)
    records_path.write_text("\n".join(lines) + "\n")
    h_tampered = _compute_content_hash(
        pack_dir,
        manifest["fixtures"],
        pack_version=int(manifest.get("pack_version", 1)),
        capture=manifest.get("capture", {}),
    )
    assert h_tampered != h_orig
    manifest["content_hash"] = h_tampered
    pack_json_path.write_text(json.dumps(manifest))
    assert verify_content_hash(pack_dir)  # the on-disk tampered variant is self-consistent at h_tampered

    import veridex.ingest.replay_pack as replay_pack_mod

    real_stream = replay_pack_mod.marketstates_from_record_stream

    def _restore_original_then_stream(records: Any, **kwargs: Any) -> Any:
        # Simulate the pack being restored to its ORIGINAL bytes mid-load — after the parse read, before a
        # separate hash read. A loader that hashes a second disk snapshot would now read the frozen hash.
        records_path.write_bytes(orig_records)
        pack_json_path.write_bytes(orig_pack_json)
        return real_stream(records, **kwargs)

    monkeypatch.setattr(replay_pack_mod, "marketstates_from_record_stream", _restore_original_then_stream)

    with pytest.raises(ReplayResolutionError) as exc:
        load_resolved_marketstates(catalog, resolved)
    assert exc.value.reason == "content_hash_drift"
