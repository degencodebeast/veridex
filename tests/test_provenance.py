from veridex.provenance import UNKNOWN_PROVENANCE, EvidenceRung


def test_evidence_rung_has_five_labels_and_unknown_fallback():
    assert EvidenceRung.TXLINE_ONLY.value == "txline-only"
    assert EvidenceRung.BACKFILLED_PRICE_HISTORY.value == "backfilled-price-history"
    assert EvidenceRung.RECORDED_LIVE_QUOTE.value == "recorded-live-quote"
    assert EvidenceRung.LIVE_FILL_RECEIPT.value == "live-fill-receipt"
    assert EvidenceRung.SYNTHETIC.value == "synthetic"
    assert UNKNOWN_PROVENANCE == "unknown-provenance"
    assert {r.value for r in EvidenceRung} == {
        "txline-only", "backfilled-price-history", "recorded-live-quote",
        "live-fill-receipt", "synthetic",
    }
