"""WD-7 — CLV sample-size confidence (REQ-054 / AC of REQ-013).

Pure, display-only mapping from ``valid_count`` (law-valid decisions) to a confidence tier and a
``low_sample`` flag. This NEVER enters any rank key: the leaderboard sorts on ``avg_clv_bps`` only
(SEC-005 / CON-006 / CON-008). A small-sample positive CLV must read as less reliable than a
large-sample one — but it is FLAGGED, never hidden or reordered.

TRUST PATH (CON-004): no LLM SDK imports.
"""

from __future__ import annotations

from typing import Any

#: Largest ``valid_count`` still considered a LOW-confidence (small) sample.
LOW_SAMPLE_MAX: int = 9
#: Largest ``valid_count`` still considered a MEDIUM-confidence sample (above this is HIGH).
MEDIUM_SAMPLE_MAX: int = 29


def clv_confidence(valid_count: int) -> dict[str, Any]:
    """Map a CLV sample size to a confidence tier + low-sample flag.

    Tiers: ``valid_count <= LOW_SAMPLE_MAX`` → ``"low"`` (``low_sample=True``);
    ``<= MEDIUM_SAMPLE_MAX`` → ``"medium"``; otherwise ``"high"``.

    Args:
        valid_count: The number of law-valid decisions backing the CLV figure.

    Returns:
        ``{"sample_size": valid_count, "clv_confidence": tier, "low_sample": bool}``.
    """
    if valid_count <= LOW_SAMPLE_MAX:
        tier = "low"
    elif valid_count <= MEDIUM_SAMPLE_MAX:
        tier = "medium"
    else:
        tier = "high"
    return {"sample_size": valid_count, "clv_confidence": tier, "low_sample": tier == "low"}
