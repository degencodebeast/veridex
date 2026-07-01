"""Proof Explainer package — the EDUCATIONAL LLM narrator that lives OUTSIDE the trust core.

This package is intentionally NOT in ``veridex.checks.build._TRUST_TARGETS`` so its own LLM
call is allowed; in exchange it imports NOTHING from any trust dir and touches the proof path
NOWHERE (enforced by :func:`veridex.verifier.import_audit.assert_no_trust_imports`).
"""

from __future__ import annotations

from veridex.explainer.glossary import GLOSSARY_DEFINITIONS
from veridex.explainer.proof_explainer import DISCLAIMER, FOOTER, explain_proof

__all__ = ["DISCLAIMER", "FOOTER", "GLOSSARY_DEFINITIONS", "explain_proof"]
