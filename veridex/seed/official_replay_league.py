"""Official Replay League seed — pinned canonical DeployConfigs + a deterministic hash (D1).

This module pins the COMPLETE canonical :class:`~veridex.deploy.preflight.DeployConfig` for the two
OFFICIAL directional agents (``baseline``, ``momentum``) — the verified-runtime-scoreable set on the
shipped ``demo_pack_real`` pack — plus a stable hash that ties the pinned configs to a schema version
so a reproducibility check can detect drift in the seeded DEPLOY CONFIGS. The hash is deliberately
config-only (it equals each deployed instance's ``config_hash``); the public identity binding is
tracked at the store/ledger layer, NOT in this hash (see :func:`seed_definition_hash`).

HONESTY BOUNDARY (Gate-1): exactly TWO official agents. No ``momentum-sharp`` /
``cumulative-drift`` (template-only on the shipped pack) / ``value-vs-venue`` (not runtime-viable) /
``llm``. Each official agent carries BOTH a public identity (``public_agent_id``) and a distinct
runtime id (``agent_id``); the public identity is attached by the seed, NOT by the DeployConfig
(which has no ``public_agent_id`` field).

The canonical config OVERRIDES ``source_mode`` to ``replay`` (off the live default) and pins the
replay SELECTION (``replay_pack_id`` / ``replay_fixture_id``); every other field takes its model
default, so ``config_hash()`` hashes the full default-expanded config exactly as the deploy route
would pin it.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from veridex.api.deploy import _build_agent
from veridex.deploy.preflight import DeployConfig
from veridex.ingest.replay_catalog import build_catalog
from veridex.ingest.replay_pack import load_pack_marketstates
from veridex.runtime.evidence import serialize_payload
from veridex.runtime.orchestrator import run_competition
from veridex.scoring import score_run

#: Schema version of the pinned canonical DeployConfig shape — bumped when the pinned config fields
#: (or their canonical serialization) change, so the seed-definition hash detects drift.
DEPLOYCONFIG_SCHEMA_VERSION = 1

#: The two competitions' fixtures the Official Replay League covers.
LEAGUE_FIXTURES = [18213979, 18222446]

#: The verified R-2 replay pack the official agents replay (the literal catalog id).
_REPLAY_PACK_ID = "demo_pack_real"

#: The pinned replay fixture the canonical config selects (first league fixture); bound to
#: ``LEAGUE_FIXTURES[0]`` so the two constants cannot drift.
_REPLAY_FIXTURE_ID = LEAGUE_FIXTURES[0]


@dataclass(frozen=True)
class OfficialAgentDef:
    """One official league agent's pinned identity + strategy.

    Attributes:
        public_agent_id: The stable PUBLIC identity attached by the seed (not a DeployConfig field).
        template_id: The strategy-archetype template the instance is configured from.
        agent_id: The distinct runtime identifier of the deployed agent.
        strategy: The strategy family (``baseline`` | ``momentum``).
        display_name: Human-facing league display name.
        idempotency_key: The seed idempotency key (stable re-seed dedupe key).
    """

    public_agent_id: str
    template_id: str
    agent_id: str
    strategy: Literal["baseline", "momentum"]
    display_name: str
    idempotency_key: str


#: The exactly-two official, verified-scoreable directional agents.
OFFICIAL_AGENTS: list[OfficialAgentDef] = [
    OfficialAgentDef(
        public_agent_id="agt_official_baseline",
        template_id="official-baseline",
        agent_id="official-baseline-v1",
        strategy="baseline",
        display_name="Official Baseline Control",
        idempotency_key="seed-official-baseline-v1",
    ),
    OfficialAgentDef(
        public_agent_id="agt_official_momentum",
        template_id="official-momentum",
        agent_id="official-momentum-v1",
        strategy="momentum",
        display_name="Official Momentum",
        idempotency_key="seed-official-momentum-v1",
    ),
]


def canonical_deploy_config(defn: OfficialAgentDef) -> DeployConfig:
    """Build the COMPLETE canonical :class:`DeployConfig` for an official agent.

    Overrides ``source_mode`` to ``replay`` (off the live default) and pins the replay selection;
    every other field takes its model default. ``public_agent_id`` is intentionally NOT set — it is
    not a DeployConfig field (identity is attached by the seed).

    Args:
        defn: The official agent definition to pin a config for.

    Returns:
        A valid, default-expanded :class:`DeployConfig` for a ``replay`` deploy.
    """
    return DeployConfig(
        template_id=defn.template_id,
        agent_id=defn.agent_id,
        strategy=defn.strategy,
        source_mode="replay",
        execution_mode="paper",
        replay_pack_id=_REPLAY_PACK_ID,
        replay_fixture_id=_REPLAY_FIXTURE_ID,
        market_allowlist=[],
        venue_allowlist=[],
        min_edge_bps=0,
        max_stake=0.0,
        mm=None,
    )


def seed_definition_hash() -> str:
    """Deterministic fingerprint of the pinned canonical DEPLOY CONFIGS.

    Hashes ``(DEPLOYCONFIG_SCHEMA_VERSION, sorted per-agent config_hash())`` via the ONE canonical
    serializer, so the digest is byte-stable across processes. Deliberately config-only: this equals
    the ``config_hash`` the deploy route pins for each deployed instance, so a reproducibility check
    can compare the shipped seed's configs against the live deployment.

    The public identity binding (``public_agent_id`` ↔ instance) is intentionally NOT hashed here —
    it is tracked in the store instance link + the seed ledger. A change to a pinned config
    (strategy, pack, fixture, knobs) moves this digest; a change to a ``display_name`` /
    ``public_agent_id`` does not (that drift is caught at the store/ledger layer).

    Returns:
        The hex SHA-256 of the canonical serialization of the pinned deploy configs.
    """
    payload = {
        "schema_version": DEPLOYCONFIG_SCHEMA_VERSION,
        "config_hashes": sorted(canonical_deploy_config(a).config_hash() for a in OFFICIAL_AGENTS),
    }
    return hashlib.sha256(serialize_payload(payload).encode("utf-8")).hexdigest()


class ScoreabilityError(RuntimeError):
    """An admitted official agent produced ZERO scored actions on the shipped pack (fail-closed).

    Raised by :func:`assert_scoreable` when the real replay pipeline proves an agent that DEPLOYS
    would silently never act — the honesty failure the guard exists to catch.
    """


async def assert_scoreable(
    agents: Sequence[OfficialAgentDef] = OFFICIAL_AGENTS,
    fixtures: Sequence[int] = LEAGUE_FIXTURES,
) -> None:
    """Run the real build→load→run→score pipeline for each agent on the shipped pack; fail closed.

    Resolves the shipped ``demo_pack_real`` pack the SAME way production does (from the env-driven
    replay catalog, never a hardcoded filesystem path), builds each agent through the ONE canonical
    deploy config, and replays every requested fixture. If any agent's TOTAL ``action_count`` across
    ``fixtures`` is 0, it deploys but silently never acts — a scoreability lie — so the guard raises
    :class:`ScoreabilityError` naming that agent. Returns ``None`` when every agent genuinely acts.

    Args:
        agents: The official agent defs to prove scoreable (defaults to :data:`OFFICIAL_AGENTS`).
        fixtures: The fixture ids to replay (defaults to :data:`LEAGUE_FIXTURES`).

    Raises:
        ScoreabilityError: If the shipped pack is absent, a requested fixture is not in the pack, or
            any admitted agent scores 0 actions across ``fixtures``.
    """
    catalog = build_catalog(os.environ.get("REPLAY_PACK_ROOT", "") or None)
    entry = catalog.get(_REPLAY_PACK_ID)
    if entry is None:
        raise ScoreabilityError(f"pack {_REPLAY_PACK_ID!r} not present in the replay catalog")
    for fid in fixtures:
        if fid not in entry.fixtures:
            raise ScoreabilityError(
                f"fixture {fid} not present in pack {_REPLAY_PACK_ID!r} (has {list(entry.fixtures)})"
            )

    built = [_build_agent(canonical_deploy_config(defn)) for defn in agents]
    totals: dict[str, int] = {defn.agent_id: 0 for defn in agents}
    for fid in fixtures:
        marketstates = load_pack_marketstates(entry.pack_dir, fid)
        run = await run_competition(marketstates, built, source_mode="replay")
        for row in score_run(run):
            totals[row["agent_id"]] += int(row["action_count"])

    for agent_id, total in totals.items():
        if total == 0:
            raise ScoreabilityError(
                f"agent {agent_id!r} scored 0 actions across fixtures {list(fixtures)} — not scoreable"
            )
