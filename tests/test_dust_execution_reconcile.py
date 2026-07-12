"""E4-T1 — durable pre-submit record persisted BEFORE the wire POST (IDM-005, AC-040).

MONEY-NETWORK BOUNDARY. Every seam here is a Mode-A fake: the wire POST is an injected async
callable (never a live venue) and the fill history is an in-memory reader. Mode B is UNARMED.

What these tests pin (§6 group 3, IDM-005 / AC-040 / AC-011):

* **Durability BEFORE the POST.** The durable compound :class:`PreSubmitRecord`
  ``{integrity_commitment_hash, venue_order_key, captured_id?}`` MUST be written to the
  append-only durable ledger BEFORE the wire POST is attempted, so an order stays identifiable
  in complete venue truth even if the ACK is lost or it never appears in open orders. The
  durability is proven by a POST callback that reads the durable ledger AT POST TIME and asserts
  the record is already there; the mutation (persist-AFTER-POST) makes that assertion fail.

* **A fresh read after "restart" recovers the record.** ``recover_presubmit_records`` reconstructs
  the compound records from the persisted rows (not the submit-time object), mirroring the E2-T2
  ``realized_fill_ledger`` restart-reconstruction pattern.

* **Reconcile by ``venue_order_key``, NEVER the private integrity digest.** The venue keys
  order/trade/fill responses by the official V2 order hash. ``test_ack_lost_fill_resolved_by_venue_order_key``
  proves a fill history that knows ONLY ``venue_order_key`` (and has NEVER seen the integrity hash)
  still resolves the ACK-lost order to ``RESOLVED``.

* **No secret in the row.** Only the one-way digest + the public venue key + non-secret refs are
  persisted — never a raw owner / L2 cred / signature.
"""

from __future__ import annotations

from typing import Any

from veridex.dust_execution.contracts import PreSubmitRecord
from veridex.dust_execution.reconcile import (
    DurableSubmitResult,
    reconcile_durable_ack_lost,
    recover_presubmit_records,
    submit_with_durable_presubmit,
)
from veridex.store import InMemoryStore, PreSubmitLedgerRow

# A UNIQUE secret sentinel that is NEVER passed into the durable ledger — the store-scan proves
# no raw owner / L2 cred / signature can appear in a persisted row.
_SECRET_SENTINEL = "UNIQUE-OWNER-UUID-Zzq9Kx7-SECRET-DO-NOT-PERSIST"

_SESSION = "dust-sess-A"
# venue_order_key is the official V2 order hash (the venue join key); the integrity digest is a
# private sha256 the venue has NEVER seen — the two are DELIBERATELY distinct.
_VENUE_ORDER_KEY = "0xdeadbeefofficialv2orderhash"
_INTEGRITY_HASH = "a" * 64  # private one-way digest, never a venue join key


def _record(*, captured_id: str | None = None) -> PreSubmitRecord:
    return PreSubmitRecord(
        integrity_commitment_hash=_INTEGRITY_HASH,
        venue_order_key=_VENUE_ORDER_KEY,
        captured_id=captured_id,
    )


# ---------------------------------------------------------------------------
# Durability-before-POST: the record is durable BEFORE the wire POST is attempted
# ---------------------------------------------------------------------------


async def test_durable_presubmit_persisted_before_wire_post() -> None:
    """The compound record is durable in the ledger BEFORE the wire POST runs.

    RED (durability-before-POST): the POST callback reads the durable ledger and asserts the
    record is ALREADY persisted. A submit path that POSTs WITHOUT first persisting would make
    this assertion fail. The mutation check (persist AFTER post) proves it.
    """
    store = InMemoryStore()
    record = _record()
    seen_at_post: dict[str, Any] = {"rows": None}

    async def _post() -> dict[str, Any]:
        # AT POST TIME the durable record MUST already exist (persist-before-POST invariant).
        rows = await store.list_presubmit(_SESSION)
        seen_at_post["rows"] = rows
        assert len(rows) == 1, "record must be durable BEFORE the wire POST is attempted"
        assert rows[0].venue_order_key == _VENUE_ORDER_KEY
        return {"success": True, "orderID": "0xVENUEACK"}

    result = await submit_with_durable_presubmit(
        store=store, session_id=_SESSION, record=record, post=_post
    )

    assert isinstance(result, DurableSubmitResult)
    assert result.presubmit_seq >= 1
    assert result.presubmit_record == record
    assert result.response == {"success": True, "orderID": "0xVENUEACK"}
    # The POST callback actually saw the durable record.
    assert seen_at_post["rows"] is not None and len(seen_at_post["rows"]) == 1


async def test_persist_before_post_ordering_is_the_first_side_effect() -> None:
    """The durable persist is the FIRST side-effect: the ledger append precedes the POST call."""
    store = InMemoryStore()
    events: list[str] = []

    orig_append = store.append_presubmit

    async def _spy_append(**kw: Any) -> int:
        events.append("persist")
        return await orig_append(**kw)

    store.append_presubmit = _spy_append  # type: ignore[method-assign]

    async def _post() -> dict[str, Any]:
        events.append("post")
        return {"success": True}

    await submit_with_durable_presubmit(
        store=store, session_id=_SESSION, record=_record(), post=_post
    )
    assert events == ["persist", "post"], f"persist MUST precede the POST, got {events}"


# ---------------------------------------------------------------------------
# Restart recovery: a fresh read over the persisted rows reconstructs the record
# ---------------------------------------------------------------------------


async def test_fresh_read_after_restart_recovers_compound_record() -> None:
    """A fresh read reconstructs the compound record from the durable rows (E2-T2 restart pattern)."""
    store = InMemoryStore()

    async def _post() -> dict[str, Any]:
        return {"success": True}

    await submit_with_durable_presubmit(
        store=store, session_id=_SESSION, record=_record(captured_id="cap-1"), post=_post
    )

    # Fresh read: rebuild PreSubmitRecord objects from the persisted rows (NOT the submit-time obj).
    recovered = await recover_presubmit_records(store, _SESSION)
    assert len(recovered) == 1
    rec = recovered[0]
    assert isinstance(rec, PreSubmitRecord)
    assert rec.venue_order_key == _VENUE_ORDER_KEY
    assert rec.integrity_commitment_hash == _INTEGRITY_HASH
    assert rec.captured_id == "cap-1"


# ---------------------------------------------------------------------------
# Reconcile by venue_order_key, NEVER the private integrity digest
# ---------------------------------------------------------------------------


async def test_ack_lost_fill_resolved_by_venue_order_key() -> None:
    """A fill history keyed ONLY by the official V2 order id resolves the ACK-lost order to RESOLVED.

    The fake venue's fill history knows ONLY ``venue_order_key`` — it has NEVER seen the private
    integrity hash. Reconciliation queries by ``venue_order_key`` and STILL resolves RESOLVED,
    proving the join key is the official venue id and never the private digest.
    """
    store = InMemoryStore()

    async def _post() -> dict[str, Any]:
        return {"success": True}

    await submit_with_durable_presubmit(
        store=store, session_id=_SESSION, record=_record(), post=_post
    )

    queried_keys: list[str] = []

    async def _fill_reader(key: str) -> dict[str, Any]:
        # The reader is keyed ONLY by the official V2 order id — record every key it is asked for.
        queried_keys.append(key)
        if key == _VENUE_ORDER_KEY:
            return {"trades": [{"taker_order_id": key, "size": 4.0, "status": "CONFIRMED"}]}
        return {"trades": []}

    reconciled = await reconcile_durable_ack_lost(store, _SESSION, _fill_reader)

    assert len(reconciled) == 1
    assert reconciled[0].venue_order_key == _VENUE_ORDER_KEY
    assert reconciled[0].reconciled_state == "RESOLVED"
    assert reconciled[0].reconciled_fill_size == 4.0
    # The reconciler queried ONLY by the official venue id — NEVER by the private integrity digest.
    assert queried_keys == [_VENUE_ORDER_KEY]
    assert _INTEGRITY_HASH not in queried_keys


async def test_ack_lost_unmatched_fill_is_ambiguous_never_fabricated() -> None:
    """No matching fill for the venue_order_key resolves fail-closed to AMBIGUOUS (never fabricated)."""
    store = InMemoryStore()

    async def _post() -> dict[str, Any]:
        return {"success": True}

    await submit_with_durable_presubmit(
        store=store, session_id=_SESSION, record=_record(), post=_post
    )

    async def _empty_reader(key: str) -> dict[str, Any]:
        return {"trades": []}

    reconciled = await reconcile_durable_ack_lost(store, _SESSION, _empty_reader)
    assert len(reconciled) == 1
    assert reconciled[0].reconciled_state == "AMBIGUOUS"
    assert reconciled[0].reconciled_fill_size == 0.0


# ---------------------------------------------------------------------------
# No secret persisted: only the digest + public venue key + non-secret refs
# ---------------------------------------------------------------------------


async def test_no_secret_persisted_in_durable_row() -> None:
    """The durable row carries ONLY the digest + public venue key + captured_id — NEVER a secret."""
    store = InMemoryStore()

    async def _post() -> dict[str, Any]:
        return {"success": True}

    await submit_with_durable_presubmit(
        store=store, session_id=_SESSION, record=_record(captured_id="cap-9"), post=_post
    )

    rows = await store.list_presubmit(_SESSION)
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, PreSubmitLedgerRow)

    # The row's field set is EXACTLY the non-secret compound record + seq/session — no owner/cred/sig.
    field_names = set(vars(row).keys())
    assert field_names == {
        "seq",
        "session_id",
        "integrity_commitment_hash",
        "venue_order_key",
        "captured_id",
    }
    # A UNIQUE secret sentinel we NEVER passed can NOT appear anywhere in the persisted row.
    assert _SECRET_SENTINEL not in repr(row)
    assert row.integrity_commitment_hash == _INTEGRITY_HASH
    assert row.venue_order_key == _VENUE_ORDER_KEY
    assert row.captured_id == "cap-9"


async def test_durable_ledger_is_append_only_across_multiple_submits() -> None:
    """Sequential submits append monotonically-seq'd rows; nothing is updated or deleted."""
    store = InMemoryStore()

    async def _post() -> dict[str, Any]:
        return {"success": True}

    r1 = await submit_with_durable_presubmit(
        store=store, session_id=_SESSION, record=_record(captured_id="a"), post=_post
    )
    r2 = await submit_with_durable_presubmit(
        store=store, session_id=_SESSION, record=_record(captured_id="b"), post=_post
    )
    assert r2.presubmit_seq > r1.presubmit_seq

    rows = await store.list_presubmit(_SESSION)
    assert [row.captured_id for row in rows] == ["a", "b"]
    assert [row.seq for row in rows] == sorted(row.seq for row in rows)
