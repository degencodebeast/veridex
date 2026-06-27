"""Phase 1 — B7 evidence-hash hardening tests (REQ-113 / AC-113, gate CON-003).

TDD: each test was watched RED (feature missing) then GREEN after implementation.

Behaviors under test:
- Duplicate sequence_no raises ValueError.
- Hash is order-independent for unique sequence_no.
- Return value is a 64-char lowercase hex string.
- Pure function: same input twice → same hash.
- The hash equals sha256(serialize_payload(sorted_events)) — canonical array form.
"""

from __future__ import annotations

import hashlib

import pytest

from veridex.runtime.evidence import compute_evidence_hash, serialize_payload

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _events_out_of_order() -> list[dict]:
    """Two events provided in reverse sequence_no order."""
    return [
        {"sequence_no": 3, "event_type": "decision", "action_payload_json": '{"type":"FLAG"}'},
        {"sequence_no": 1, "event_type": "tick", "state_snapshot_json": '{"tick":1}'},
        {"sequence_no": 2, "event_type": "tick", "state_snapshot_json": '{"tick":2}'},
    ]


# ---------------------------------------------------------------------------
# B7-1: Duplicate sequence_no must raise ValueError
# ---------------------------------------------------------------------------


def test_duplicate_sequence_no_raises_value_error():
    """compute_evidence_hash must raise ValueError when two events share sequence_no."""
    events = [
        {"sequence_no": 1, "event_type": "tick"},
        {"sequence_no": 1, "event_type": "decision"},  # duplicate!
    ]
    with pytest.raises(ValueError, match="duplicate sequence_no"):
        compute_evidence_hash(events)


def test_duplicate_sequence_no_non_adjacent_raises():
    """Duplicate detection must work even when duplicates are not adjacent after sort."""
    events = [
        {"sequence_no": 1, "event_type": "tick"},
        {"sequence_no": 2, "event_type": "tick"},
        {"sequence_no": 1, "event_type": "decision"},  # duplicate, non-adjacent in input
    ]
    with pytest.raises(ValueError, match="duplicate sequence_no"):
        compute_evidence_hash(events)


# ---------------------------------------------------------------------------
# B7-2: Order-independent for unique sequence_no
# ---------------------------------------------------------------------------


def test_order_independent_for_unique_seq():
    """Hash must be the same regardless of input ordering (sort by sequence_no)."""
    events = _events_out_of_order()
    h_forward = compute_evidence_hash(events)
    h_reversed = compute_evidence_hash(list(reversed(events)))
    assert h_forward == h_reversed


# ---------------------------------------------------------------------------
# B7-3: Returns 64-char lowercase sha256 hex string
# ---------------------------------------------------------------------------


def test_returns_64_char_lowercase_hex():
    """Return value must be a 64-character lowercase hex string."""
    events = [{"sequence_no": 1, "event_type": "tick"}]
    h = compute_evidence_hash(events)
    assert isinstance(h, str)
    assert len(h) == 64
    assert h == h.lower()
    # only hex characters
    assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# B7-4: Pure function — determinism (same input → same hash)
# ---------------------------------------------------------------------------


def test_determinism_same_input_produces_same_hash():
    """Calling compute_evidence_hash twice with equal input must return equal hashes."""
    events = _events_out_of_order()
    assert compute_evidence_hash(events) == compute_evidence_hash(events)


# ---------------------------------------------------------------------------
# B7-5: Canonical array hash — pin the exact formula
# ---------------------------------------------------------------------------


def test_canonical_array_hash_matches_formula():
    """Hash must equal sha256(serialize_payload(sorted_events).encode('utf-8')).hexdigest().

    This pins the canonical-array form: a single JSON array serialised with sorted keys
    and compact separators, NOT a concatenation of per-event strings.
    """
    events = _events_out_of_order()
    sorted_events = sorted(events, key=lambda e: e["sequence_no"])
    expected = hashlib.sha256(serialize_payload(sorted_events).encode("utf-8")).hexdigest()
    assert compute_evidence_hash(events) == expected
