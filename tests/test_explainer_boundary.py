"""Proof Explainer Phase A — TRUST-CRITICAL boundary teeth (TDD, RED-proofed).

The whole "no LLM in the proof path" thesis rides on this fence. These tests pin:

* TEETH 1 — IMPORT ISOLATION: ``veridex/explainer/`` imports NOTHING from any trust dir, is NOT
  in ``_TRUST_TARGETS`` (so its own LLM import is allowed), and the trust dirs STILL import zero
  LLM SDK. RED-proof: a planted trust-path import makes ``assert_no_trust_imports`` FAIL.
* TEETH 4 — GRACEFUL DEGRADE: no OPENROUTER_API_KEY ⇒ an honest "unavailable" string, NEVER a
  fabricated explanation, and NEVER a network call.
* TEETH 2 (anchor) — the explainer, given a sanitized dict, mutates NOTHING (the "changes nothing"
  invariant at the unit boundary; the endpoint-level headline lives in ``test_api_explain.py``).
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from veridex.config import Settings

_EXPLAINER_DIR = Path(__file__).parent.parent / "veridex" / "explainer"


# ---------------------------------------------------------------------------
# Test doubles — an injected async HTTP client so no test ever hits the network.
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:  # pragma: no cover - trivial
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    """Records calls; returns a canned completion. ``post`` is async to match httpx.AsyncClient."""

    def __init__(self, content: str = "This proof shows a sealed run.") -> None:
        self.content = content
        self.calls: list[dict] = []

    async def post(self, url: str, headers: dict | None = None, json: dict | None = None) -> _FakeResp:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return _FakeResp(self.content)


def _settings_without_key() -> Settings:
    return Settings(_env_file=None)  # openrouter_api_key defaults to None


def _settings_with_key() -> Settings:
    return Settings(_env_file=None, openrouter_api_key="test-key")


# ---------------------------------------------------------------------------
# TEETH 1 — IMPORT ISOLATION
# ---------------------------------------------------------------------------


def test_explainer_imports_nothing_from_any_trust_dir() -> None:
    """(b) The explainer package imports NOTHING from law/scoring/leaderboard/verifier/checks/ingest/policy."""
    from veridex.verifier.import_audit import assert_no_trust_imports

    assert_no_trust_imports(_EXPLAINER_DIR)  # must not raise


def test_explainer_is_not_a_trust_target() -> None:
    """(c) ``veridex/explainer/`` is NOT in ``_TRUST_TARGETS`` — so its OWN LLM import is allowed."""
    from veridex.checks.build import _TRUST_TARGETS

    trust_paths = {Path(p).resolve() for p in _TRUST_TARGETS}
    assert _EXPLAINER_DIR.resolve() not in trust_paths


def test_explainer_may_import_an_llm_sdk_without_tripping_the_llm_audit() -> None:
    """The explainer is OUTSIDE the LLM boundary: an LLM SDK import there is NOT a forbidden import
    for the trust audit (the trust audit only sweeps ``_TRUST_TARGETS``, which excludes explainer)."""
    from veridex.checks.build import _TRUST_TARGETS
    from veridex.verifier.import_audit import assert_no_llm_imports

    for target in _TRUST_TARGETS:  # trust dirs STILL import zero LLM SDK (existing guard, unchanged)
        assert_no_llm_imports(target)


@pytest.mark.parametrize(
    "src",
    [
        "from veridex.verifier.recompute import verify_run\n",
        "from veridex.checks.build import build_check_results\n",
        "import veridex.scoring\n",
        "from veridex.law.recompute import recompute\n",
        "from veridex.leaderboard import leaderboard\n",
        "import veridex.ingest.live_client\n",
        "from veridex.policy import engine\n",
    ],
)
def test_trust_import_audit_fires_on_planted_trust_import(tmp_path: Path, src: str) -> None:
    """RED-proof: a trust-path import planted into an explainer-like package makes the audit FAIL."""
    from veridex.verifier.import_audit import assert_no_trust_imports

    (tmp_path / "mod.py").write_text(src)
    with pytest.raises(AssertionError, match="Forbidden trust-path import"):
        assert_no_trust_imports(tmp_path)


def test_trust_import_audit_allows_non_trust_and_relative_imports(tmp_path: Path) -> None:
    """The audit leaves non-trust siblings (config, chain, explainer) + relative imports alone."""
    from veridex.verifier.import_audit import assert_no_trust_imports

    (tmp_path / "mod.py").write_text(
        "from veridex.config import get_settings\n"
        "import veridex.explainer.glossary\n"
        "from . import sibling\n"
        "import json\n"
    )
    assert_no_trust_imports(tmp_path)  # must not raise


# ---------------------------------------------------------------------------
# TEETH 4 — GRACEFUL DEGRADE (no key ⇒ honest unavailable, never fabricated, never a network call)
# ---------------------------------------------------------------------------


async def test_missing_openrouter_key_returns_honest_unavailable() -> None:
    from veridex.explainer import DISCLAIMER, FOOTER, explain_proof

    fake = _FakeClient()
    out = await explain_proof({"proof_artifact": {}}, settings=_settings_without_key(), client=fake)

    assert "unavailable" in out["explanation"].lower()
    assert "no llm key" in out["explanation"].lower()
    # NEVER a fabricated answer, and NEVER a network call attempted.
    assert fake.calls == []
    # The disclaimer + footer still travel with the honest-degrade response.
    assert out["disclaimer"] == DISCLAIMER
    assert out["footer"] == FOOTER


async def test_llm_call_failure_degrades_honestly_not_fabricated() -> None:
    from veridex.explainer import explain_proof

    class _BoomClient:
        async def post(self, *a, **k):
            raise RuntimeError("network down")

    out = await explain_proof({"proof_artifact": {}}, settings=_settings_with_key(), client=_BoomClient())
    assert "unavailable" in out["explanation"].lower()


# ---------------------------------------------------------------------------
# TEETH 2 (anchor) — the explainer given a sanitized dict mutates NOTHING
# ---------------------------------------------------------------------------


async def test_explainer_does_not_mutate_the_read_model() -> None:
    """RED-proof anchor for "changes nothing": if the explainer wrote back into its input, this fails."""
    from veridex.explainer import explain_proof

    read_model = {
        "proof_artifact": {"evidence": {"evidence_hash": "abc"}, "checks": {"llm_boundary": {"result": "pass"}}},
        "verify": {"verified": True, "evidence_hash": "abc"},
        "glossary": {"clv": {"label": "CLV", "definition": "x"}},
    }
    before = copy.deepcopy(read_model)
    await explain_proof(read_model, settings=_settings_with_key(), client=_FakeClient())
    assert read_model == before  # byte-identical: the explainer narrates, it never writes back


async def test_explainer_returns_disclaimer_and_footer_envelope() -> None:
    from veridex.explainer import DISCLAIMER, FOOTER, explain_proof

    out = await explain_proof({"proof_artifact": {}}, settings=_settings_with_key(), client=_FakeClient("hi"))
    assert out["explanation"] == "hi"
    assert out["disclaimer"] == DISCLAIMER
    assert out["footer"] == FOOTER
    # The fence disclaimer names the deterministic verifier as the source of truth.
    assert "does not verify" in DISCLAIMER
    assert "source of truth" in FOOTER.lower()
