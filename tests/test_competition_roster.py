"""I-7 — Strategy-aware roster construction + roster->instance identity binding (TDD, LOAD-BEARING).

The competition roster must build the DECLARED contestants (by strategy, position-independent), fail
CLOSED on an unknown strategy (never a silent baseline/contrarian substitution), and — the trust core
— run the ACTUAL Studio-deployed contestant when a roster entry references a deployed instance (pinned
``instance_id`` + ``config_hash``), never a freshly-reconstructed same-named look-alike. A drift between
the deployed instance and the roster's pinned identity is CAUGHT (fail-closed), not silently run.

All offline, zero network. RED5 reuses the I-1/I-2/I-7b locally-signed Privy ES256 harness to prove the
I-7b owner-gate on ``/start`` is preserved by the roster change.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport

from veridex.api.demo_fixtures import bind_roster_instance, build_agents_from_roster
from veridex.api.router import create_app
from veridex.competition.models import AgentEntry
from veridex.config import Settings
from veridex.deploy.instance import AgentInstance
from veridex.runtime.orchestrator import PROOF_MODE_LLM, PROOF_MODE_REPRODUCIBLE
from veridex.store import InMemoryStore
from veridex_agent.config import AgentRunConfig


def _entry(
    agent_id: str,
    strategy: str,
    *,
    instance_id: str | None = None,
    config_hash: str | None = None,
    model: str | None = None,
) -> AgentEntry:
    """Build a roster :class:`AgentEntry` (proof_mode already normalised)."""
    return AgentEntry(
        agent_id=agent_id,
        owner="team",
        strategy=strategy,
        model=model,
        proof_mode="reproducible",
        config_hash=config_hash,
        instance_id=instance_id,
    )


# ---------------------------------------------------------------------------
# RED 1 — the DECLARED roster is built by strategy (NOT positional baseline/contrarian).
# ---------------------------------------------------------------------------


async def test_declared_roster_builds_by_strategy() -> None:
    entries = [_entry("a", "cumulative-drift"), _entry("b", "llm")]

    agents = await build_agents_from_roster(entries)

    assert [a.agent_id for a in agents] == ["a", "b"]
    # The DECLARED strategies: cumulative-drift is reproducible-proof, llm is evidence-verified.
    # The OLD positional builder would make BOTH "reproducible" (deterministic + contrarian) — a lie.
    assert agents[0].proof_mode == PROOF_MODE_REPRODUCIBLE
    assert agents[1].proof_mode == PROOF_MODE_LLM


# ---------------------------------------------------------------------------
# RED 2 — position-independence: swapping roster order does not change WHICH agents build.
# ---------------------------------------------------------------------------


async def test_roster_build_is_position_independent() -> None:
    swapped = [_entry("b", "llm"), _entry("a", "cumulative-drift")]

    agents = await build_agents_from_roster(swapped)

    # The llm agent is evidence-verified regardless of position; cumulative-drift reproducible
    # regardless of position. The old positional builder would flip proof modes with the order.
    assert agents[0].agent_id == "b"
    assert agents[0].proof_mode == PROOF_MODE_LLM
    assert agents[1].agent_id == "a"
    assert agents[1].proof_mode == PROOF_MODE_REPRODUCIBLE


# ---------------------------------------------------------------------------
# RED 3 — unknown strategy fails CLOSED (explicit error, never a silent substitution).
# ---------------------------------------------------------------------------


async def test_unknown_strategy_fails_closed() -> None:
    entries = [_entry("a", "cumulative-drift"), _entry("b", "totally-unknown-strategy")]

    with pytest.raises(ValueError) as exc:
        await build_agents_from_roster(entries)

    # The error must NAME the offending strategy — it must not silently fall back to a baseline.
    assert "totally-unknown-strategy" in str(exc.value)


# ---------------------------------------------------------------------------
# RED 4 — roster->instance identity binding (the trust core): run the ACTUAL deployed contestant;
#         drift is caught, not silently run.
# ---------------------------------------------------------------------------


def _deployed_instance(*, instance_id: str, config_hash: str) -> AgentInstance:
    """A deployed instance whose effective_config has NON-DEFAULT knobs (so a naive strategy-label
    reconstruction produces a DIFFERENT config identity than the actual deployed config)."""
    effective = AgentRunConfig(
        agent_id="deployed-contestant",
        strategy="momentum-sharp",
        lookback=48,  # non-default (default is 8) — a reconstruction from the label alone misses this
        z_threshold=3.3,  # non-default (default 2.5)
    ).model_dump(mode="json")
    return AgentInstance(
        instance_id=instance_id,
        template_id="sharp-momentum-v2",
        agent_id="deployed-contestant",
        submitted_config=effective,
        effective_config=effective,
        config_hash=config_hash,
        policy_hash="pol-hash",
        source_mode="replay",
        execution_mode="paper",
        run_id="run-x",
        operator_id="did:privy:ALICE",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


async def test_roster_instance_binding_runs_deployed_config() -> None:
    instance = _deployed_instance(instance_id="inst_1", config_hash="PINNED_HASH")
    store = InMemoryStore()
    await store.persist_agent_instance(instance)

    # The roster entry PINS the deployed instance (instance_id + its config_hash). Its strategy LABEL
    # is deliberately "baseline" — a reconstruction from the label would build a DIFFERENT agent.
    entry = _entry("roster-name", "baseline", instance_id="inst_1", config_hash="PINNED_HASH")

    # The pure binding resolves to the ACTUAL deployed config, NOT a label reconstruction.
    bound = bind_roster_instance(entry, instance)
    deployed_cfg = AgentRunConfig.model_validate(instance.effective_config)
    reconstruction = AgentRunConfig(agent_id=entry.agent_id, strategy="baseline")
    assert bound.config_hash() == deployed_cfg.config_hash()
    assert bound.config_hash() != reconstruction.config_hash()

    # End-to-end: the built agent IS the deployed contestant (its agent_id comes from the deployed
    # effective_config, not the roster label).
    agents = await build_agents_from_roster([entry], get_instance=store.get_agent_instance)
    assert len(agents) == 1
    assert agents[0].agent_id == "deployed-contestant"


async def test_roster_instance_config_drift_is_caught() -> None:
    instance = _deployed_instance(instance_id="inst_1", config_hash="PINNED_HASH")
    store = InMemoryStore()
    await store.persist_agent_instance(instance)

    # The roster pinned a STALE hash: the live deployed instance's config drifted away from it.
    drifted = _entry("roster-name", "momentum-sharp", instance_id="inst_1", config_hash="STALE_HASH")

    # Fail-closed: the drift is CAUGHT (the actual deployed identity != the pinned one) — never run.
    with pytest.raises(ValueError) as pure_exc:
        bind_roster_instance(drifted, instance)
    assert "drift" in str(pure_exc.value).lower()

    with pytest.raises(ValueError):
        await build_agents_from_roster([drifted], get_instance=store.get_agent_instance)


# ---------------------------------------------------------------------------
# RED 5 — I-7b auth preserved (regression): starting a competition still requires the Privy owner.
# ---------------------------------------------------------------------------

_APP_ID = "test-privy-app-id"
_DID_ALICE = "did:privy:ALICE"
_DID_BOB = "did:privy:BOB"


def _make_keypair() -> tuple[str, str]:
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = (
        priv.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return priv_pem, pub_pem


_PRIV_PEM, _PUB_PEM = _make_keypair()


def _sign(*, sub: str) -> str:
    now = int(time.time())
    claims = {"sub": sub, "aud": _APP_ID, "iss": "privy.io", "iat": now, "exp": now + 3600, "sid": "sess-1"}
    return jwt.encode(claims, _PRIV_PEM, algorithm="ES256")


def _bearer(did: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_sign(sub=did)}"}


def _privy_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="development",
        auth_mode="privy",
        privy_app_id=_APP_ID,
        privy_verification_key=_PUB_PEM,
    )


def _transport(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _drain(app: Any) -> None:
    tasks = list(getattr(app.state, "deploy_background_tasks", set()))
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


_COMPETITION_CONFIG: dict[str, Any] = {
    "competition_type": "replay_arena",
    "source_mode": "replay",
    "execution_mode": "paper",
    "market_scope": "WC:TEST",
    "roster_size": 2,
}


def _reg_body(agent_id: str, strategy: str) -> dict[str, Any]:
    return {"agent_id": agent_id, "owner": "team", "strategy": strategy, "model": None, "proof_mode": "reproducible"}


async def test_start_still_requires_owner_after_roster_change() -> None:
    app = create_app(store=InMemoryStore(), settings=_privy_settings())
    try:
        async with _transport(app) as client:
            create = await client.post("/competitions", json=_COMPETITION_CONFIG, headers=_bearer(_DID_ALICE))
            assert create.status_code == 200, create.text
            comp_id = create.json()["competition_id"]
            # Register a real DECLARED roster (exercises the new by-strategy build path under auth).
            for agent_id, strategy in (("a", "cumulative-drift"), ("b", "baseline")):
                reg = await client.post(
                    f"/competitions/{comp_id}/agents", json=_reg_body(agent_id, strategy), headers=_bearer(_DID_ALICE)
                )
                assert reg.status_code == 200, reg.text

            # Anonymous start -> 401 (auth precedes any roster build).
            anon = await client.post(f"/competitions/{comp_id}/start")
            assert anon.status_code == 401, anon.text

            # Non-owner start -> 403 (I-7b owner-gate intact, not weakened by the roster change).
            bob = await client.post(f"/competitions/{comp_id}/start", headers=_bearer(_DID_BOB))
            assert bob.status_code == 403, bob.text

            # The owner CAN start, and the declared roster runs to a finalized leaderboard.
            owner = await client.post(f"/competitions/{comp_id}/start", headers=_bearer(_DID_ALICE))
            assert owner.status_code == 200, owner.text
            assert owner.json()["status"] == "finalized"
    finally:
        await _drain(app)
