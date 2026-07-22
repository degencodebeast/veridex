"""D1 — Official Replay League seed definitions: pinned canonical DeployConfigs + hash.

The two verified-scoreable official directional agents (``baseline``, ``momentum``) are pinned
here with their COMPLETE canonical :class:`DeployConfig` and a deterministic seed-definition hash.
Honesty boundary: exactly TWO official agents — no momentum-sharp / cumulative-drift /
value-vs-venue / llm (none are verified-runtime-scoreable on the shipped pack).
"""

from __future__ import annotations

from veridex.deploy.preflight import DeployConfig
from veridex.seed.official_replay_league import (
    DEPLOYCONFIG_SCHEMA_VERSION,
    LEAGUE_FIXTURES,
    OFFICIAL_AGENTS,
    OfficialAgentDef,
    canonical_deploy_config,
    seed_definition_hash,
)


def _by_strategy(strategy: str) -> OfficialAgentDef:
    return next(a for a in OFFICIAL_AGENTS if a.strategy == strategy)


def test_exactly_two_official_agents_with_pinned_identity() -> None:
    assert len(OFFICIAL_AGENTS) == 2
    strategies = {a.strategy for a in OFFICIAL_AGENTS}
    assert strategies == {"baseline", "momentum"}

    # Honesty: the non-verified-scoreable families are NOT in the official set.
    for excluded in ("momentum-sharp", "cumulative-drift", "value-vs-venue", "llm"):
        assert excluded not in strategies

    baseline = _by_strategy("baseline")
    assert baseline.public_agent_id == "agt_official_baseline"
    assert baseline.template_id == "official-baseline"
    assert baseline.agent_id == "official-baseline-v1"
    assert baseline.display_name == "Official Baseline Control"
    assert baseline.idempotency_key == "seed-official-baseline-v1"

    momentum = _by_strategy("momentum")
    assert momentum.public_agent_id == "agt_official_momentum"
    assert momentum.template_id == "official-momentum"
    assert momentum.agent_id == "official-momentum-v1"
    assert momentum.display_name == "Official Momentum"
    assert momentum.idempotency_key == "seed-official-momentum-v1"


def test_league_fixtures_and_schema_version_pinned() -> None:
    assert LEAGUE_FIXTURES == [18213979, 18222446]
    assert DEPLOYCONFIG_SCHEMA_VERSION == 1


def test_official_runtime_and_public_ids_distinct() -> None:
    # Gate-1 boundary: distinct runtime ids AND distinct public ids across the official set.
    assert len({a.agent_id for a in OFFICIAL_AGENTS}) == 2
    assert len({a.public_agent_id for a in OFFICIAL_AGENTS}) == 2


def test_canonical_deploy_config_shape() -> None:
    baseline = _by_strategy("baseline")
    config = canonical_deploy_config(baseline)
    assert isinstance(config, DeployConfig)
    # source_mode MUST be overridden off the live default.
    assert config.source_mode == "replay"
    assert config.strategy == "baseline"
    assert config.replay_pack_id == "demo_pack_real"
    assert config.execution_mode == "paper"
    # public_agent_id is NOT a DeployConfig field — identity is attached by the seed, not here.
    assert "public_agent_id" not in config.model_dump()


def test_canonical_deploy_config_hash_matches_fresh_build() -> None:
    baseline = _by_strategy("baseline")
    fresh = DeployConfig(
        template_id="official-baseline",
        agent_id="official-baseline-v1",
        strategy="baseline",
        source_mode="replay",
        execution_mode="paper",
        replay_pack_id="demo_pack_real",
        replay_fixture_id=18213979,
        market_allowlist=[],
        venue_allowlist=[],
        min_edge_bps=0,
        max_stake=0.0,
        mm=None,
    )
    assert canonical_deploy_config(baseline).config_hash() == fresh.config_hash()


#: Golden digest of the pinned canonical DEPLOY CONFIGS (config-only, by design).
#: Pinned from d19a0d8's shipped configs; a change to any pinned config (or the schema
#: version) moves this digest and fails the test. A display_name / public_agent_id change
#: does NOT move it (that drift is caught at the store/ledger layer, not this hash).
_GOLDEN_SEED_DEFINITION_HASH = "107a86f680686214b672cf40d5b52ebffc042b5aeacc164144119e463564ccf7"


def test_seed_definition_hash_matches_golden_pin() -> None:
    # Determinism (byte-stable across calls) AND the golden config-only fingerprint.
    assert seed_definition_hash() == seed_definition_hash()
    assert seed_definition_hash() == _GOLDEN_SEED_DEFINITION_HASH
