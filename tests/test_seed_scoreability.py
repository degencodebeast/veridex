"""D2 — scoreability guard: the official league agents must genuinely act on the shipped pack.

The honesty check that would have caught a config that deploys but silently NEVER acts. Each test
runs the REAL build→load→run→score pipeline against the bundled ``demo_pack_real`` pack (mounted by
the autouse ``_replay_pack_root`` conftest fixture), so a "scoreable" verdict is earned from a real
run, not asserted from config shape.

The first two tests were watched RED (``assert_scoreable`` / ``ScoreabilityError`` did not exist)
before the guard was implemented, then GREEN.

  * positive — ``assert_scoreable()`` returns ``None`` for the two pinned agents (``baseline`` +
    ``momentum``) across ``LEAGUE_FIXTURES``: both genuinely score > 0 actions.
  * negative — a GENUINELY inert def (strategy ``momentum-sharp`` on the shipped pack, which is
    template-only there and produces ``action_count == 0`` — verified by running the pipeline
    directly) makes the guard raise ``ScoreabilityError`` naming the agent.
  * negative, strategy-independent — an empty fixtures list deterministically drives ``totals`` to
    0 with no run at all, proving the zero-total raise branch without depending on any strategy's
    runtime behavior.
"""

from __future__ import annotations

import pytest

from veridex.seed.official_replay_league import (
    OFFICIAL_AGENTS,
    OfficialAgentDef,
    ScoreabilityError,
    assert_scoreable,
)

_INERT_FIXTURE = 18213979


async def test_official_agents_are_scoreable() -> None:
    """The pinned official agents genuinely act on the shipped pack — no raise, returns ``None``."""
    await assert_scoreable()  # returns None / does not raise == both official agents are scoreable


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
        strategy="momentum-sharp",  # type: ignore[arg-type]  # intentionally off-Literal: a genuinely inert built agent (scores 0 on the shipped tape) proves the guard fires
        display_name="Inert Momentum-Sharp (test-only)",
        idempotency_key="test-inert-momentum-sharp-v1",
    )
    with pytest.raises(ScoreabilityError, match="inert-momentum-sharp-v1"):
        await assert_scoreable([inert], [_INERT_FIXTURE])


async def test_empty_fixtures_raises_scoreability_error() -> None:
    """Zero-total raise branch, strategy-independent: no fixtures ⇒ 0 actions ⇒ raise."""
    with pytest.raises(ScoreabilityError):
        await assert_scoreable([OFFICIAL_AGENTS[0]], [])
