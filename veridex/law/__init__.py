"""Veridex deterministic law (trust path — LLM-free).

The law recomputes edge / CLV / Kelly / validation from evidence only; the LLM never
self-certifies (CON-001). This package MUST NOT import any LLM SDK (CON-007) — enforced
by the import audit (`veridex.verifier.import_audit`).
"""

from __future__ import annotations

from veridex.law.recompute import LIVE, REPLAY, recompute

__all__ = ["recompute", "REPLAY", "LIVE"]
