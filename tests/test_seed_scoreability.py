"""D2 — scoreability guard: the official league agents must genuinely act on the shipped pack.

The honesty check that would have caught a config that deploys but silently NEVER acts. Each test
runs the REAL build→load→run→score pipeline against the bundled ``demo_pack_real`` pack (mounted by
the autouse ``_replay_pack_root`` conftest fixture), so a "scoreable" verdict is earned from a real
run, not asserted from config shape.

Both tests were watched RED (``assert_scoreable`` / ``ScoreabilityError`` did not exist) before the
guard was implemented, then GREEN.

  * positive — ``assert_scoreable()`` returns ``None`` for the two pinned agents (``baseline`` +
    ``momentum``) across ``LEAGUE_FIXTURES``: both genuinely score > 0 actions.
  * negative — a GENUINELY inert def (strategy ``momentum-sharp`` on the shipped pack, which is
    template-only there and produces ``action_count == 0`` — verified by running the pipeline
    directly) makes the guard raise ``ScoreabilityError`` naming the agent.
"""

from __future__ import annotations

import pytest

from veridex.seed.official_replay_league import (
    OfficialAgentDef,
    ScoreabilityError,
    assert_scoreable,
)

_INERT_FIXTURE = 18213979


async def test_official_agents_are_scoreable() -> None:
    """The pinned official agents genuinely act on the shipped pack — no raise, returns ``None``."""
    assert await assert_scoreable() is None


async def test_inert_agent_raises_scoreability_error() -> None:
    """A def whose built agent genuinely scores 0 actions trips the fail-closed guard.

    ``momentum-sharp`` with the canonical (default) knobs is template-only on ``demo_pack_real`` —
    it abstains on fixture 18213979 (empirically ``action_count == 0``). Running the guard over
    ONLY this inert def must raise ``ScoreabilityError`` naming the agent. The def is NOT added to
    ``OFFICIAL_AGENTS``; it exists solely to prove the guard fires on a real zero-action agent.
    """
    inert = OfficialAgentDef(
        public_agent_id="agt_inert_momentum_sharp",
        template_id="inert-momentum-sharp",
        agent_id="inert-momentum-sharp-v1",
        strategy="momentum-sharp",  # not an OFFICIAL strategy; genuinely abstains on this pack
        display_name="Inert Momentum-Sharp (test-only)",
        idempotency_key="test-inert-momentum-sharp-v1",
    )
    with pytest.raises(ScoreabilityError, match="inert-momentum-sharp-v1"):
        await assert_scoreable([inert], [_INERT_FIXTURE])
