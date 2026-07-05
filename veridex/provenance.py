"""Shared evidence-rung enum; supersedes the provenance-string copy in scripts/demo_phase2d.py."""

from enum import Enum


class EvidenceRung(str, Enum):
    TXLINE_ONLY = "txline-only"
    BACKFILLED_PRICE_HISTORY = "backfilled-price-history"
    RECORDED_LIVE_QUOTE = "recorded-live-quote"
    LIVE_FILL_RECEIPT = "live-fill-receipt"
    SYNTHETIC = "synthetic"


UNKNOWN_PROVENANCE = "unknown-provenance"
