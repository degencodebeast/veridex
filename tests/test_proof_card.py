"""B8 proof-card enrichment tests — TDD-strict.

Written before the enriched implementation (RED), turned GREEN by the minimal
``build_proof_card`` extension. The Phase-0 contract test
(``test_proof_card_public_json_uses_checks_not_cats``) lives in
``test_phase0_red.py`` and MUST stay green throughout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# ── helpers ───────────────────────────────────────────────────────────────────


def _all_keys(obj: Any) -> list[str]:
    """Recursively collect every dict key (as strings) from *obj*."""
    if not isinstance(obj, dict):
        return []
    keys: list[str] = []
    for k, v in obj.items():
        keys.append(str(k))
        keys.extend(_all_keys(v))
    return keys


def _minimal_card(**overrides: Any) -> dict[str, Any]:
    """Build a proof card using minimal required arguments plus any ``overrides``."""
    from veridex.verifier.proof_card import build_proof_card

    defaults: dict[str, Any] = {
        "run": {"run_id": "r1", "source_mode": "replay"},
        "evidence": {"evidence_hash": "ab" * 32, "run_event_count": 4},
        "checks": {"clv": {"result": "pass"}},
        "proof_mode": "reproducible",
    }
    defaults.update(overrides)
    return build_proof_card(**defaults)


_FIXTURE = Path(__file__).parent / "fixtures" / "txline_odds_sample.json"


# ── B8-T01: verifier_version ──────────────────────────────────────────────────


def test_card_has_verifier_version() -> None:
    """Card includes a non-empty ``verifier_version`` string (AC-111)."""
    card = _minimal_card()
    assert "verifier_version" in card
    assert isinstance(card["verifier_version"], str)
    assert card["verifier_version"]  # non-empty


# ── B8-T02: lineage.proof_mode_map ───────────────────────────────────────────


def test_card_lineage_has_proof_mode_map() -> None:
    """``lineage.proof_mode_map`` reflects the explicit agent→proof-mode mapping."""
    card = _minimal_card(proof_mode_map={"agent-det": "reproducible"})
    assert "lineage" in card
    assert "proof_mode_map" in card["lineage"]
    assert card["lineage"]["proof_mode_map"] == {"agent-det": "reproducible"}


# ── B8-T03: lineage.schema_versions ──────────────────────────────────────────


def test_card_lineage_has_schema_versions() -> None:
    """``lineage.schema_versions`` is a non-empty dict when defaults are used."""
    card = _minimal_card()
    assert "lineage" in card
    sv = card["lineage"]["schema_versions"]
    assert isinstance(sv, dict)
    assert len(sv) > 0


# ── B8-T04: anchor.status ─────────────────────────────────────────────────────


def test_card_has_anchor_status() -> None:
    """``anchor.status`` is one of the three recognised values."""
    card = _minimal_card()
    assert "anchor" in card
    assert "status" in card["anchor"]
    assert card["anchor"]["status"] in ("anchored", "pending", "not_anchored")


# ── B8-T05: checks present, cats never a key (recursive) ─────────────────────


def test_checks_present_cats_never_a_key() -> None:
    """``checks`` appears as a key; the substring ``cats`` never appears in any key name."""
    card = _minimal_card(checks={"clv": {"result": "pass"}, "kelly": {"result": "warn"}})
    all_keys = _all_keys(card)
    assert "checks" in all_keys
    for key in all_keys:
        assert "cats" not in key, f"Forbidden key containing 'cats' found: {key!r}"


# ── B8-T06a: anchor pending ───────────────────────────────────────────────────


def test_anchor_status_pending() -> None:
    """Anchor block with status ``pending`` and no signature is preserved exactly."""
    card = _minimal_card(anchor={"status": "pending", "signature": None, "cluster": "devnet"})
    assert card["anchor"]["status"] == "pending"
    assert card["anchor"]["signature"] is None


# ── B8-T06b: anchor anchored with signature ───────────────────────────────────


def test_anchor_status_anchored_with_signature() -> None:
    """Anchor block with status ``anchored`` carries the tx signature through."""
    sig = "5J6NkZ" + "x" * 80
    card = _minimal_card(anchor={"status": "anchored", "signature": sig, "cluster": "devnet"})
    assert card["anchor"]["status"] == "anchored"
    assert card["anchor"]["signature"] == sig


# ── B8-T06c: default anchor is not_anchored ───────────────────────────────────


def test_anchor_status_not_anchored_default() -> None:
    """When no ``anchor`` kwarg is provided the card defaults to ``not_anchored``."""
    card = _minimal_card()
    assert card["anchor"]["status"] == "not_anchored"
    assert card["anchor"]["signature"] is None


# ── B8-T07: from RunResult ────────────────────────────────────────────────────


async def test_proof_card_from_run_result_uses_run_shape() -> None:
    """Building from a real ``run_competition`` RunResult embeds proof_mode_map and evidence_hash."""
    from veridex.ingest.marketstate import replay_marketstates
    from veridex.runtime.orchestrator import RunResult, deterministic_agent, run_competition
    from veridex.verifier.proof_card import proof_card_from_run_result

    marketstates = replay_marketstates(str(_FIXTURE))
    agent = deterministic_agent()
    result: RunResult = await run_competition(marketstates, [agent], source_mode="replay")

    card = proof_card_from_run_result(result, checks={"clv": {"result": "pass"}})

    assert card["run"]["run_id"] == result.run_id
    assert card["lineage"]["proof_mode_map"] == result.proof_mode_map
    assert card["evidence"]["evidence_hash"] == result.evidence_hash
    assert "checks" in card
    for key in _all_keys(card):
        assert "cats" not in key, f"Forbidden key containing 'cats': {key!r}"


# ── B8-T08: import-audit clean over verifier/ ─────────────────────────────────


def test_import_audit_clean_over_verifier() -> None:
    """The verifier trust path must remain free of LLM SDK imports after B8 enrichment."""
    import veridex.verifier as verifier_pkg
    from veridex.verifier.import_audit import assert_no_llm_imports

    assert_no_llm_imports(Path(verifier_pkg.__file__).parent)
