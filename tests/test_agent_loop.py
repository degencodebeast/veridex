"""B4 — LLM agent loop (Agno). Offline TDD suite — NO real LLM call; agno is mocked.

`emit_agent_action` / `emit_agent_action_async` are the ONLY veridex functions that import
`agno`, and they import it LAZILY (inside the call, behind an injectable factory/model seam)
so that:
  * `import veridex.runtime.agent` works WITHOUT agno installed,
  * the offline suite never imports an LLM SDK and never hits the network,
  * the import-audited trust path (`checks/ verifier/ law/ ingest/`) stays clean.

B4 is STATELESS, snapshot-only: the only context is the `MarketState(<=t)` snapshot. No
run-state, history, memory, or learning (those are B5/Phase 2). B4 returns the action only —
it writes NO DB rows, proof cards, score rows, anchors, or evidence hashes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pydantic
import pytest

from veridex.ingest.marketstate import MarketState
from veridex.runtime.schemas import AgentAction, SportsActionType


def _market_state() -> MarketState:
    return MarketState(
        fixture_id=17952170,
        tick_seq=0,
        ts=1718000000,
        phase=2,
        markets={"OU_2_5": {"stable_prob_bps": 5800, "stable_price": 1.72, "suspended": False}},
        scores={"home": 1, "away": 0},
    )


class _FakeResponse:
    """Stand-in for Agno's RunOutput; only `.content` is read by emit_agent_action."""

    def __init__(self, content):
        self.content = content


class _FakeAgent:
    def __init__(self, content):
        self._content = content
        self.run_prompts: list = []

    def run(self, prompt, **kwargs):
        self.run_prompts.append(prompt)
        return _FakeResponse(self._content)


class _FakeAsyncAgent:
    """Async stand-in for Agno's Agent; exposes `arun` for use by emit_agent_action_async."""

    def __init__(self, content):
        self._content = content
        self.arun_prompts: list = []

    async def arun(self, prompt, **kwargs):
        self.arun_prompts.append(prompt)
        return _FakeResponse(self._content)


def _spy_factory(content, recorder: dict | None = None):
    """Return an agent_factory that records its construction kwargs and yields a FakeAgent."""

    def factory(**kwargs):
        if recorder is not None:
            recorder["construct_kwargs"] = kwargs
        return _FakeAgent(content)

    return factory


def _async_spy_factory(content, recorder: dict | None = None):
    """Return an agent_factory that records its construction kwargs and yields a FakeAsyncAgent."""

    def factory(**kwargs):
        if recorder is not None:
            recorder["construct_kwargs"] = kwargs
        return _FakeAsyncAgent(content)

    return factory


# Sentinel model so the default (agno-importing) model builder is never reached offline.
_SENTINEL_MODEL = object()


# ---------------------------------------------------------------------------
# SYNC tests (emit_agent_action)
# ---------------------------------------------------------------------------


# 1 — structured path: response.content is already an AgentAction -> returned as-is.
def test_emit_returns_agent_action_when_content_is_typed():
    from veridex.runtime.agent import emit_agent_action

    expected = AgentAction(type=SportsActionType.FLAG_VALUE, params={"market": "OU_2_5"})
    action = emit_agent_action(_market_state(), model=_SENTINEL_MODEL, agent_factory=_spy_factory(expected))
    assert isinstance(action, AgentAction)
    assert action is expected
    assert action.type == SportsActionType.FLAG_VALUE


# 2 — fallback path: provider returns raw JSON text -> parse_agent_action_json validates it.
def test_emit_falls_back_to_json_parse_when_content_is_text():
    from veridex.runtime.agent import emit_agent_action

    raw = json.dumps({"type": "WAIT", "params": {"reason": "quiet", "confidence": 0.4}})
    action = emit_agent_action(_market_state(), model=_SENTINEL_MODEL, agent_factory=_spy_factory(raw))
    assert isinstance(action, AgentAction)
    assert action.type == SportsActionType.WAIT
    # rationale/confidence survive as UNTRUSTED params metadata (gate 1 may ignore them).
    assert action.params["confidence"] == 0.4


# 2b — dict path: provider returns a raw dict -> isinstance(content, dict) -> model_validate.
def test_emit_validates_dict_content():
    from veridex.runtime.agent import emit_agent_action

    raw = {"type": "WAIT", "params": {"confidence": 0.4}}
    action = emit_agent_action(_market_state(), model=_SENTINEL_MODEL, agent_factory=_spy_factory(raw))
    assert isinstance(action, AgentAction)
    assert action.type == SportsActionType.WAIT
    assert action.params["confidence"] == 0.4


# 2c — clean error: content is None (or non-str/non-dict) -> ValueError, not a confusing
# TypeError out of json.loads(None). Keeps the trust boundary's failure mode legible.
def test_emit_raises_clean_value_error_on_none_content():
    from veridex.runtime.agent import emit_agent_action

    with pytest.raises(ValueError, match="no parseable content"):
        emit_agent_action(_market_state(), model=_SENTINEL_MODEL, agent_factory=_spy_factory(None))


# 3 — HARD invariant: the Agent is constructed with tools=[] (decision-only, no execution).
def test_emit_constructs_agent_with_empty_tools():
    from veridex.runtime.agent import emit_agent_action

    recorder: dict = {}
    expected = AgentAction(type=SportsActionType.WAIT)
    emit_agent_action(
        _market_state(),
        model=_SENTINEL_MODEL,
        agent_factory=_spy_factory(expected, recorder),
    )
    kwargs = recorder["construct_kwargs"]
    assert kwargs["tools"] == []
    assert kwargs["model"] is _SENTINEL_MODEL
    # output_schema requested so Agno can return a validated pydantic object.
    assert kwargs["output_schema"] is AgentAction


# 3b — prefer_output_schema=False -> the Agent is built with output_schema=None.
def test_emit_disables_output_schema_when_not_preferred():
    from veridex.runtime.agent import emit_agent_action

    recorder: dict = {}
    raw = json.dumps({"type": "WAIT", "params": {}})
    emit_agent_action(
        _market_state(),
        prefer_output_schema=False,
        model=_SENTINEL_MODEL,
        agent_factory=_spy_factory(raw, recorder),
    )
    assert recorder["construct_kwargs"]["output_schema"] is None


# 4 — over-powered / malformed action text fails AgentAction validation (raises).
def test_emit_raises_on_overpowered_action_text():
    from veridex.runtime.agent import emit_agent_action

    # "EXECUTE_TRADE" is not in the constrained SportsActionType enum -> must reject.
    rogue = json.dumps({"type": "EXECUTE_TRADE", "params": {"size": 9999}})
    with pytest.raises(pydantic.ValidationError):
        emit_agent_action(_market_state(), model=_SENTINEL_MODEL, agent_factory=_spy_factory(rogue))


# 5 — agent_config_hash is deterministic + stable (B5 records it as evidence later).
def test_agent_config_hash_is_deterministic_and_stable():
    from veridex.runtime.agent import agent_config_hash

    h1 = agent_config_hash("claude-sonnet-4-6", "prompt-A", "sports_v0")
    h2 = agent_config_hash("claude-sonnet-4-6", "prompt-A", "sports_v0")
    assert h1 == h2
    assert isinstance(h1, str) and len(h1) == 64  # sha256 hex
    # different inputs -> different hash
    assert h1 != agent_config_hash("claude-sonnet-4-6", "prompt-B", "sports_v0")
    assert h1 != agent_config_hash("claude-opus-4-6", "prompt-A", "sports_v0")
    assert h1 != agent_config_hash("claude-sonnet-4-6", "prompt-A", "sports_v1")


# 6 — the prompt declares rationale/confidence/claimed-edge as UNTRUSTED metadata (gate 1).
def test_prompt_marks_rationale_confidence_edge_as_untrusted():
    from veridex.runtime.agent import build_decision_prompt

    prompt = build_decision_prompt(_market_state())
    lowered = prompt.lower()
    assert "untrusted" in lowered
    # the MarketState snapshot is serialized into the prompt (the only context this pass).
    assert "OU_2_5" in prompt
    # all allowed action types are advertised so the model stays inside the constrained set.
    for action in SportsActionType:
        assert action.value in prompt


# 7 — import veridex.runtime.agent succeeds WITHOUT agno and fires no network call (lazy import).
def test_agent_module_imports_without_agno_and_no_network():
    import veridex.runtime.agent  # noqa: F401 — import itself is the assertion

    # agno is imported lazily inside emit_agent_action; merely importing the module (and
    # running it with injected seams) must NOT pull agno into the process.
    assert "agno" not in sys.modules
    # a full emit_agent_action run via injected fake factory + sentinel model stays offline.
    from veridex.runtime.agent import emit_agent_action

    emit_agent_action(
        _market_state(),
        model=_SENTINEL_MODEL,
        agent_factory=_spy_factory(AgentAction(type=SportsActionType.WAIT)),
    )
    assert "agno" not in sys.modules


# 8 — trust-path import audit stays clean: agent.py (agno) must not leak into the trust path.
def test_trust_path_imports_no_llm_sdk():
    import veridex.checks as checks_pkg
    import veridex.ingest as ingest_pkg
    import veridex.law as law_pkg
    import veridex.verifier as verifier_pkg
    from veridex.verifier.import_audit import assert_no_llm_imports

    for pkg in (checks_pkg, verifier_pkg, law_pkg, ingest_pkg):
        assert_no_llm_imports(Path(pkg.__file__).parent)


# 9 — (creds-gated, default-skipped) live smoke against a real model key.
@pytest.mark.skipif(
    not (os.getenv("VERIDEX_LIVE_AGNO") and os.getenv("ANTHROPIC_API_KEY")),
    reason="live Agno smoke: set VERIDEX_LIVE_AGNO=1 and ANTHROPIC_API_KEY",
)
def test_live_emit_agent_action_smoke():
    from veridex.runtime.agent import emit_agent_action

    action = emit_agent_action(_market_state())
    assert isinstance(action, AgentAction)
    assert action.type in set(SportsActionType)


# ---------------------------------------------------------------------------
# ASYNC tests (emit_agent_action_async) — B4-async (Bit B4)
# ---------------------------------------------------------------------------


# A1 — structured path: response.content is already an AgentAction -> returned as-is.
async def test_async_emit_returns_agent_action_when_content_is_typed():
    from veridex.runtime.agent import emit_agent_action_async

    expected = AgentAction(type=SportsActionType.FLAG_VALUE, params={"market": "OU_2_5"})
    action = await emit_agent_action_async(
        _market_state(), model=_SENTINEL_MODEL, agent_factory=_async_spy_factory(expected)
    )
    assert isinstance(action, AgentAction)
    assert action is expected
    assert action.type == SportsActionType.FLAG_VALUE


# A2 — fallback path: provider returns raw JSON text -> parse_agent_action_json validates it.
async def test_async_emit_falls_back_to_json_parse_when_content_is_text():
    from veridex.runtime.agent import emit_agent_action_async

    raw = json.dumps({"type": "WAIT", "params": {"reason": "quiet", "confidence": 0.4}})
    action = await emit_agent_action_async(
        _market_state(), model=_SENTINEL_MODEL, agent_factory=_async_spy_factory(raw)
    )
    assert isinstance(action, AgentAction)
    assert action.type == SportsActionType.WAIT
    assert action.params["confidence"] == 0.4


# A3 — dict path: provider returns a raw dict -> isinstance(content, dict) -> model_validate.
async def test_async_emit_validates_dict_content():
    from veridex.runtime.agent import emit_agent_action_async

    raw = {"type": "WAIT", "params": {"confidence": 0.4}}
    action = await emit_agent_action_async(
        _market_state(), model=_SENTINEL_MODEL, agent_factory=_async_spy_factory(raw)
    )
    assert isinstance(action, AgentAction)
    assert action.type == SportsActionType.WAIT
    assert action.params["confidence"] == 0.4


# A4 — clean error: content is None (or non-str/non-dict) -> ValueError, not TypeError.
async def test_async_emit_raises_clean_value_error_on_none_content():
    from veridex.runtime.agent import emit_agent_action_async

    with pytest.raises(ValueError, match="no parseable content"):
        await emit_agent_action_async(_market_state(), model=_SENTINEL_MODEL, agent_factory=_async_spy_factory(None))


# A5 — HARD invariant: the Agent is constructed with tools=[] (decision-only, no execution).
async def test_async_emit_constructs_agent_with_empty_tools():
    from veridex.runtime.agent import emit_agent_action_async

    recorder: dict = {}
    expected = AgentAction(type=SportsActionType.WAIT)
    await emit_agent_action_async(
        _market_state(),
        model=_SENTINEL_MODEL,
        agent_factory=_async_spy_factory(expected, recorder),
    )
    kwargs = recorder["construct_kwargs"]
    assert kwargs["tools"] == []
    assert kwargs["model"] is _SENTINEL_MODEL
    assert kwargs["output_schema"] is AgentAction


# A6 — model_id resolves from config when not passed; the resolved id flows to _default_model.
async def test_async_model_id_resolves_from_config(monkeypatch):
    from veridex.runtime import agent as agent_mod
    from veridex.runtime.agent import emit_agent_action_async

    # Patch get_settings to return a fake with a distinctive model_id.
    class _FakeSettings:
        model_id = "test-model-from-config"

    monkeypatch.setattr(agent_mod, "get_settings", lambda: _FakeSettings())

    # Patch _default_model to capture the resolved model_id without importing agno.
    captured: dict = {}

    def _fake_default_model(model_id: str):
        captured["model_id"] = model_id
        return _SENTINEL_MODEL

    monkeypatch.setattr(agent_mod, "_default_model", _fake_default_model)

    expected = AgentAction(type=SportsActionType.WAIT)
    # model=None (default) triggers _default_model with the config-resolved id.
    await emit_agent_action_async(
        _market_state(),
        agent_factory=_async_spy_factory(expected),
        # model_id intentionally omitted — must resolve from patched get_settings().
    )

    assert captured["model_id"] == "test-model-from-config"
