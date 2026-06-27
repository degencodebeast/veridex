"""Tests for normalize_proof_mode — Phase 2A Task 3.

Covers the wire→canonical mapping for proof_mode values at the Phase-2A boundary.
REQ-219 / AC-216: normalised value must be exactly ``"reproducible"`` or ``"verified"``.
"""

import pytest

from veridex.competition.models import normalize_proof_mode


def test_reproducible_passthrough():
    assert normalize_proof_mode("reproducible") == "reproducible"


def test_llm_label_normalizes():
    assert normalize_proof_mode("LLM/evidence-verified") == "verified"


def test_verified_passthrough():
    assert normalize_proof_mode("verified") == "verified"


def test_unknown_rejected():
    with pytest.raises(ValueError):
        normalize_proof_mode("totally-unknown")


def test_empty_string_rejected():
    with pytest.raises(ValueError):
        normalize_proof_mode("")
