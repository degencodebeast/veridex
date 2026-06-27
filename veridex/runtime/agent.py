"""LLM/Agno decision layer (lives OUTSIDE the verifier/trust path — gate 7).

Behavior is test-driven (T3). Stubs raise NotImplementedError. NOTE: this module does NOT
import agno at T0 (agno is added in T3 GREEN); the trust path must never import it.
"""
from __future__ import annotations

import json
from typing import Any

from veridex.runtime.schemas import AgentAction


def emit_agent_action(market_state: Any, *, prefer_output_schema: bool = True) -> AgentAction:
    """Agno `Agent(tools=[], output_schema=AgentAction)`; falls back to JSON-parse if needed.

    Deferred to live wiring: this is the only path that imports `agno` and makes a provider
    call, so it needs creds + network and has no offline test (like the devnet SSE smoke).
    Phase 0 proves the boundary via the JSON-parse fallback below + the import audit; the live
    Agno call is exercised in Phase 1, not here.
    """
    raise NotImplementedError("emit_agent_action needs live Agno creds — wired in Phase 1, not Phase 0")


def parse_agent_action_json(text: str) -> AgentAction:
    """JSON-parse fallback → validated AgentAction (when provider output_schema is unavailable).

    This is the trust-relevant path: whatever an LLM emits as text is parsed and *re-validated*
    against the constrained `AgentAction` schema before anything downstream sees it.
    """
    return AgentAction.model_validate(json.loads(text))
