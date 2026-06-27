"""LLM/Agno decision layer (lives OUTSIDE the verifier/trust path — gate 7).

B4 — the LLM agent loop. STATELESS, snapshot-only: the only context an agent gets is the
`MarketState(<=t)` snapshot. NO run-state, history, memory, conversations, or learning
(those are B5/Phase 2). `emit_agent_action` returns the decision ONLY — it writes NO DB rows,
proof cards, score rows, anchors, or evidence hashes.

This is the ONLY veridex module that touches `agno`, and it imports it LAZILY (inside the
call, behind an injectable factory/model seam). Therefore `import veridex.runtime.agent`
works WITHOUT agno installed and fires no network call; the import-audited trust path
(`checks/ verifier/ law/ ingest/`) never imports an LLM SDK.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Optional

from veridex.runtime.schemas import AgentAction, SportsActionType

# Default Claude model id for this project (overridable via `model_id=`). Verified against
# the current Agno Anthropic model surface (agno.models.anthropic.Claude).
DEFAULT_MODEL_ID = "claude-sonnet-4-6"

# Schema version stamped into the config hash so B5 can record prompt/model/schema identity
# as evidence. Matches the `action_schema` version used elsewhere in the evidence records.
AGENT_ACTION_SCHEMA_VERSION = "sports_v0"

# The constrained action vocabulary advertised to the model.
ALLOWED_ACTION_TYPES: tuple[str, ...] = tuple(a.value for a in SportsActionType)


def _serialize_market_state(market_state: Any) -> str:
    """Deterministic JSON of the snapshot (sorted keys) — the only context the agent sees."""
    if hasattr(market_state, "model_dump"):
        payload = market_state.model_dump()
    elif isinstance(market_state, dict):
        payload = market_state
    else:
        payload = {"state": repr(market_state)}
    return json.dumps(payload, sort_keys=True, default=str)


def build_decision_prompt(market_state: Any) -> str:
    """Build the decision prompt from a MarketState snapshot.

    The prompt MUST declare rationale/confidence/claimed-edge as UNTRUSTED metadata the
    deterministic verifier may ignore (gate 1) — the LLM has no execution authority and no
    say over how it is scored.
    """
    state_json = _serialize_market_state(market_state)
    allowed = ", ".join(ALLOWED_ACTION_TYPES)
    return (
        "You are a sports in-play trading decision agent operating on a single, immutable "
        "market snapshot. You have NO history, NO memory, and NO tools — this snapshot is the "
        "only context you get.\n\n"
        f"Allowed action types (choose EXACTLY ONE): {allowed}.\n\n"
        "MarketState snapshot (data up to the current tick; never any future rows):\n"
        f"{state_json}\n\n"
        "Return a single AgentAction JSON object with fields {type, params}, where `type` is "
        "one of the allowed action types above.\n\n"
        "UNTRUSTED METADATA: any rationale/reason, confidence, or claimed edge you place in "
        "`params` is UX-only narration. The deterministic verifier MAY IGNORE it entirely "
        "(gate 1); it recomputes edge/CLV from evidence and will NOT trust or score your "
        "claimed numbers. Do not assume your stated confidence or edge affects the outcome."
    )


def agent_config_hash(
    model_id: str,
    prompt: str,
    schema_version: str = AGENT_ACTION_SCHEMA_VERSION,
) -> str:
    """Deterministic sha256 hex of (model_id, prompt, schema_version).

    Captures the prompt/config/model identity so B5 can record it as evidence even though B4
    only returns the action now. Stable across processes (canonical sorted-key JSON).
    """
    canonical = json.dumps(
        {"model_id": model_id, "prompt": prompt, "schema_version": schema_version},
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _default_model(model_id: str) -> Any:
    """Lazily build the Agno Claude model (temperature=0). Imports agno ONLY when called."""
    from agno.models.anthropic import Claude  # lazy: keeps the module agno-free at import

    return Claude(id=model_id, temperature=0)


def _default_agent_factory(*, model: Any, tools: list, output_schema: Optional[type]) -> Any:
    """Lazily construct an Agno Agent. Imports agno ONLY when called.

    `tools=[]` is a HARD invariant (decision-only, no execution authority) and `markdown=False`
    keeps output parseable.
    """
    from agno.agent import Agent  # lazy: keeps the module agno-free at import

    return Agent(model=model, tools=tools, output_schema=output_schema, markdown=False)


def emit_agent_action(
    market_state: Any,
    *,
    prefer_output_schema: bool = True,
    model: Any = None,
    model_id: str = DEFAULT_MODEL_ID,
    agent_factory: Optional[Callable[..., Any]] = None,
) -> AgentAction:
    """Run the LLM decision pass over a MarketState snapshot → a validated `AgentAction`.

    Builds an Agno `Agent(model=<Claude>, tools=[], output_schema=AgentAction)` and prompts it
    with the serialized snapshot. If `response.content` is already an `AgentAction` (Agno
    honored `output_schema`), it is returned; otherwise the raw provider output is re-validated
    through the constrained schema (`parse_agent_action_json` for text, `model_validate` for a
    dict). Either way nothing reaches a caller until it passes `AgentAction` validation.

    The `model=` / `agent_factory=` seams are injectable so offline tests never import agno or
    hit the network. `tools=[]` is a HARD invariant. This function writes NO evidence/DB rows.
    """
    factory = agent_factory or _default_agent_factory
    if model is None:
        model = _default_model(model_id)

    prompt = build_decision_prompt(market_state)
    output_schema = AgentAction if prefer_output_schema else None

    agent = factory(model=model, tools=[], output_schema=output_schema)
    response = agent.run(prompt)
    content = getattr(response, "content", None)

    if isinstance(content, AgentAction):
        return content
    if isinstance(content, dict):
        return AgentAction.model_validate(content)
    return parse_agent_action_json(content)


def parse_agent_action_json(text: str) -> AgentAction:
    """JSON-parse fallback → validated AgentAction (when provider output_schema is unavailable).

    This is the trust-relevant path: whatever an LLM emits as text is parsed and *re-validated*
    against the constrained `AgentAction` schema before anything downstream sees it. A
    None/non-str payload (e.g. an empty provider response) raises a clear `ValueError` rather
    than a confusing `TypeError` out of `json.loads`.
    """
    if not isinstance(text, str):
        raise ValueError("agent returned no parseable content")
    return AgentAction.model_validate(json.loads(text))
