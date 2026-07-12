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

from dataclasses import dataclass
from typing import Any

from veridex.dust_execution.contracts import PreSubmitRecord
from veridex.dust_execution.emergency import DustSafetySession, SafetyController
from veridex.dust_execution.reconcile import (
    AmbiguousResolution,
    DurableSubmitResult,
    UncertainSubmitState,
    freezes_new_submits,
    reconcile_durable_ack_lost,
    reconcile_uncertain_submit,
    recover_presubmit_records,
    resolve_ambiguous_submit,
    retry_authorized_while,
    submit_with_durable_presubmit,
    worst_case_uncertain_exposure,
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


# ===========================================================================
# E4-T2 — tri-state uncertain-submit reconciliation vs COMPLETE VENUE TRUTH
# (IDM-002, AC-011/035, §6 group 3). The double-exposure guard.
# ===========================================================================
#
# MONEY-NETWORK BOUNDARY. ``reconcile_uncertain_submit`` queries the E3-T2 read surfaces
# (get_orders ∪ get_order-by-id ∪ get_fill_history), keyed by the OFFICIAL venue_order_key,
# BEFORE any retry. ``FakeAdapter`` is a Mode-A fake that structurally matches
# ``veridex.venues.base.VenueReconciliationReads`` (the same three async reads the real
# ``PolymarketAdapter`` exposes) — no network, no signing; Mode B UNARMED.
#
# The classifier is MUTUALLY EXCLUSIVE — exactly one of RESOLVED / DEFINITIVELY_ABSENT /
# AMBIGUOUS. RESOLVED requires POSITIVE terminal evidence (a fill in history keyed by
# venue_order_key, OR a terminal get_order status). Bare zero-open — or a missing/unavailable
# history surface — is AMBIGUOUS (fail-closed), NEVER DEFINITIVELY_ABSENT (that positive-evidence
# path is deferred to E4-T6). AMBIGUOUS FREEZES new submits and reserves worst-case exposure.

# The venue keys order/trade/fill responses by the OFFICIAL V2 order hash — never Veridex's
# private client_order_id (which is never on the wire).
REC = _record()

# A sentinel meaning "the history surface is UNAVAILABLE" (reader raises), distinct from an
# available-but-empty history ([]).
_HISTORY_UNAVAILABLE = object()


@dataclass(frozen=True)
class Fill:
    """A realized own fill keyed by the OFFICIAL venue order hash (never the private id)."""

    order_hash: str
    filled: float


def _open_order(order_id: str, *, status: str = "LIVE") -> dict[str, Any]:
    """A §5 OpenOrder record. ``client_order_id`` is DELIBERATELY absent — it is never on the wire."""
    return {
        "id": order_id,
        "status": status,
        "market": "0xcond",
        "asset_id": "111",
        "side": "BUY",
        "original_size": "10",
        "size_matched": "0",
        "price": "0.42",
    }


class FakeAdapter:
    """Mode-A fake of ``veridex.venues.base.VenueReconciliationReads`` — the complete venue truth.

    Exposes the E3-T2 read surfaces (all async, no network): ``get_orders`` (is it resting?),
    ``get_order`` (status-by-id), ``get_fill_history`` (did it fill?). Every surface touched is
    recorded in ``queried`` so "all three queried before any retry" is provable.

    Args:
        open_orders: The §5 open-order records ``get_orders`` returns (resting candidates).
        fill_history: ``Fill`` records ``get_fill_history`` maps to §3 trade rows; pass
            ``_HISTORY_UNAVAILABLE`` to make the surface raise (fail-closed test).
        status_by_id: id -> §5 status record for ``get_order``; ``None`` means "no record" (the
            ACK-lost order is not resting under that id → no terminal status).
    """

    def __init__(
        self,
        *,
        open_orders: list[dict[str, Any]] | None = None,
        fill_history: list[Fill] | object | None = None,
        status_by_id: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._open_orders = open_orders or []
        self._fill_history = [] if fill_history is None else fill_history
        self._status_by_id = status_by_id
        self.queried: list[str] = []

    async def get_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.queried.append("get_orders")
        return [dict(o) for o in self._open_orders]

    async def get_order(self, order_id: str, **kwargs: Any) -> dict[str, Any]:
        self.queried.append("get_order")
        if self._status_by_id is None:
            return {}
        return dict(self._status_by_id.get(order_id, {}))

    async def get_fill_history(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.queried.append("get_fill_history")
        if self._fill_history is _HISTORY_UNAVAILABLE:
            raise RuntimeError("fill-history surface unavailable")
        # §3 Trade EXACT-SET shape, keyed by the official taker_order_id (the venue_order_key).
        return [
            {"taker_order_id": f.order_hash, "size": str(f.filled), "status": "CONFIRMED"}
            for f in self._fill_history  # type: ignore[union-attr]
        ]


# --- The three verbatim RED tests (the double-exposure guard) --------------------------------


async def test_ack_lost_fak_that_filled_is_RESOLVED_not_retried() -> None:
    # FAK filled before the poll: absent from get_orders but present in fill history.
    adapter = FakeAdapter(
        open_orders=[], fill_history=[Fill(order_hash=REC.venue_order_key, filled=1.0)]
    )
    # NEVER "did not land": a fill keyed by the official id is POSITIVE terminal evidence.
    assert await reconcile_uncertain_submit(REC, adapter=adapter) == "RESOLVED"


async def test_zero_open_without_terminal_proof_is_AMBIGUOUS_not_absent() -> None:
    # Zero open, empty history, no status record → NO positive evidence and NO proof of absence.
    adapter = FakeAdapter(open_orders=[], fill_history=[], status_by_id=None)
    # fail-closed, no retry — "I see no open order" is NOT proof the order never existed.
    assert await reconcile_uncertain_submit(REC, adapter=adapter) == "AMBIGUOUS"


async def test_multiple_candidates_is_AMBIGUOUS() -> None:
    o1 = _open_order("0xcand1")
    o2 = _open_order("0xcand2")
    # client_order_id is never on the wire → the two open orders are indistinguishable candidates.
    adapter = FakeAdapter(open_orders=[o1, o2])
    assert await reconcile_uncertain_submit(REC, adapter=adapter) == "AMBIGUOUS"


# --- Fail-closed on a MISSING history surface (taker stays AMBIGUOUS, never absent) ----------


async def test_missing_history_surface_keeps_taker_AMBIGUOUS_never_absent() -> None:
    # The history surface is UNAVAILABLE (raises). A taker with no proof of a fill can NOT be
    # declared DEFINITIVELY_ABSENT — fail-closed to AMBIGUOUS.
    adapter = FakeAdapter(open_orders=[], fill_history=_HISTORY_UNAVAILABLE, status_by_id=None)
    verdict = await reconcile_uncertain_submit(REC, adapter=adapter)
    assert verdict == "AMBIGUOUS"
    assert verdict != "DEFINITIVELY_ABSENT"


async def test_adapter_lacking_history_surface_is_AMBIGUOUS() -> None:
    # An adapter that does not even EXPOSE get_fill_history is fail-closed (never absent).
    class _NoHistoryAdapter:
        async def get_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
            return []

        async def get_order(self, order_id: str, **kwargs: Any) -> dict[str, Any]:
            return {}

    assert await reconcile_uncertain_submit(REC, adapter=_NoHistoryAdapter()) == "AMBIGUOUS"


# --- Positive terminal evidence via the get_order status-by-id surface → RESOLVED ------------


async def test_terminal_status_via_get_order_is_RESOLVED() -> None:
    # No fill row and no open order, but get_order-by-id reports a TERMINAL status (killed/expired
    # etc.) — that is positive terminal evidence → RESOLVED, never "did not land".
    adapter = FakeAdapter(
        open_orders=[],
        fill_history=[],
        status_by_id={REC.venue_order_key: {"id": REC.venue_order_key, "status": "filled"}},
    )
    assert await reconcile_uncertain_submit(REC, adapter=adapter) == "RESOLVED"


# --- ALL THREE surfaces are queried, keyed by venue_order_key, BEFORE any retry --------------


async def test_all_three_surfaces_queried_by_venue_order_key_before_retry() -> None:
    adapter = FakeAdapter(open_orders=[], fill_history=[], status_by_id=None)
    await reconcile_uncertain_submit(REC, adapter=adapter)
    assert set(adapter.queried) == {"get_orders", "get_order", "get_fill_history"}


# --- Mutual exclusivity: the verdict is always exactly one of the closed tri-state -----------


async def test_tristate_is_mutually_exclusive() -> None:
    resolved = await reconcile_uncertain_submit(
        REC, adapter=FakeAdapter(fill_history=[Fill(order_hash=REC.venue_order_key, filled=2.0)])
    )
    ambiguous = await reconcile_uncertain_submit(REC, adapter=FakeAdapter(open_orders=[]))
    for verdict in (resolved, ambiguous):
        assert verdict in {"RESOLVED", "DEFINITIVELY_ABSENT", "AMBIGUOUS"}
    # This task NEVER fabricates DEFINITIVELY_ABSENT (its positive-evidence path is deferred to E4-T6).
    assert resolved == "RESOLVED"
    assert ambiguous == "AMBIGUOUS"


# --- AMBIGUOUS freezes new submits and reserves worst-case exposure (no double-commit) -------


def test_AMBIGUOUS_freezes_new_submits_and_reserves_worst_case_exposure() -> None:
    # AMBIGUOUS FREEZES new submits (no retry) — a possibly-live order must not be re-sent.
    assert freezes_new_submits("AMBIGUOUS") is True
    assert freezes_new_submits("RESOLVED") is False
    assert freezes_new_submits("DEFINITIVELY_ABSENT") is False

    # Worst-case exposure: a possibly-live AMBIGUOUS order reserves the FULL intended size against
    # caps + non-crossing (so it is never double-committed). Absent reserves nothing; a RESOLVED
    # order's real matched size is accounted by its reconciled record → uncertainty reserve released.
    assert worst_case_uncertain_exposure("AMBIGUOUS", intended_size=7.5) == 7.5
    assert worst_case_uncertain_exposure("DEFINITIVELY_ABSENT", intended_size=7.5) == 0.0
    assert worst_case_uncertain_exposure("RESOLVED", intended_size=7.5) == 0.0


# ===========================================================================
# E4-T3 — AMBIGUOUS freeze + bounded venue-truth polling + cancel-all timeout
# fallback (IDM-002, AC-011, §6 group 3). The no-blind-retry safety loop.
# ===========================================================================
#
# MONEY-NETWORK BOUNDARY. The loop re-runs the E4-T2 three-surface reconcile on an INJECTED clock +
# INJECTED async sleep (deterministic, fully offline): each poll RE-QUERIES venue truth by
# venue_order_key — it does NOT re-submit. On timeout it falls back to the E2-T3
# ``SafetyController.cancel_all_and_block`` sweep (+ manual operator escalation) — NEVER a blind
# retry, which is the exact double-exposure bug this guards. Mode B UNARMED.


class _InjectedClock:
    """A deterministic monotonic clock (integer ms) + async sleep that advances IT, not wall time.

    ``sleep(seconds)`` advances the injected ``now_ms`` by exactly that many ms and records the
    request — so the bounded poll runs fully offline with no real ``asyncio.sleep`` and the timeout
    boundary is exactly reproducible.
    """

    def __init__(self, *, start_ms: int = 0) -> None:
        self.now_ms = start_ms
        self.sleeps: list[float] = []

    def clock(self) -> int:
        return self.now_ms

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now_ms += int(round(seconds * 1000))


class _CancelAllSpy:
    """Mode-A fake of the E2-T3 ``CancelAllAdapter`` — counts the ONE cancel-all sweep wire."""

    def __init__(self, *, canceled: int = 2) -> None:
        self.cancel_all_calls = 0
        self._canceled = canceled

    async def cancel_all_orders(self) -> int:
        self.cancel_all_calls += 1
        return self._canceled


class _ScriptedFillAdapter(FakeAdapter):
    """AMBIGUOUS until the Nth ``get_fill_history`` re-query, when the fill appears (re-query resolves).

    Proves the poll genuinely RE-QUERIES venue truth by ``venue_order_key`` across ticks (it does NOT
    re-submit): the FAK is absent from open orders / history on early polls, then lands in fill
    history on poll ``resolve_on_call`` — flipping the real reconcile AMBIGUOUS → RESOLVED.
    """

    def __init__(self, *, resolve_on_call: int, order_hash: str) -> None:
        super().__init__(open_orders=[], fill_history=[], status_by_id=None)
        self._resolve_on_call = resolve_on_call
        self._order_hash = order_hash
        self._history_calls = 0

    async def get_fill_history(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._history_calls += 1
        self.queried.append("get_fill_history")
        if self._history_calls >= self._resolve_on_call:
            return [{"taker_order_id": self._order_hash, "size": "3.0", "status": "CONFIRMED"}]
        return []


# --- RED anchor: an AMBIGUOUS state that authorizes a retry BEFORE timeout → assertion fails ---


def test_retry_never_authorized_while_AMBIGUOUS_before_timeout() -> None:
    """A possibly-live AMBIGUOUS order is FROZEN: no retry is authorized before the timeout elapses.

    The double-exposure guard. A blind re-submit of an AMBIGUOUS order risks a double fill, so it is
    authorized in NEITHER phase — not while the poll is still running (``before_timeout=True``,
    frozen) and not when it elapses (``before_timeout=False``, the fallback is cancel-all, never a
    retry). The mutation that makes AMBIGUOUS authorize a retry before timeout fails HERE.
    """
    assert retry_authorized_while("AMBIGUOUS", before_timeout=True) is False
    assert retry_authorized_while("AMBIGUOUS", before_timeout=False) is False
    # A resolved / definitively-absent order is no longer frozen — the wallet/market is free again.
    assert retry_authorized_while("RESOLVED", before_timeout=True) is True
    assert retry_authorized_while("DEFINITIVELY_ABSENT", before_timeout=True) is True


# --- Bounded poll on an injected clock: N polls then timeout → cancel-all, NEVER a resubmit ----


async def test_bounded_poll_times_out_to_cancel_all_no_resubmit() -> None:
    """Persistently AMBIGUOUS: the bounded poll times out and falls back to the cancel-all SWEEP.

    Injected clock: the reconcile stays AMBIGUOUS every tick, the poll runs a bounded number of
    times, then the timeout elapses and the loop fires the E2-T3 ``cancel_all_and_block`` primitive
    ONCE (+ blocks new submits) — it NEVER blindly re-POSTs the order. The mutation that blind-retries
    on timeout (instead of sweeping) fails HERE.
    """
    adapter = FakeAdapter(open_orders=[], fill_history=[], status_by_id=None)  # always AMBIGUOUS
    clk = _InjectedClock()
    cancel_spy = _CancelAllSpy(canceled=3)
    controller = SafetyController(clock_ms=clk.clock)
    session = DustSafetySession("dust-sess-ambig")

    result = await resolve_ambiguous_submit(
        REC,
        adapter=adapter,
        controller=controller,
        session=session,
        cancel_adapter=cancel_spy,
        clock_ms=clk.clock,
        sleep=clk.sleep,
        poll_interval_ms=100,
        timeout_ms=300,
    )

    assert isinstance(result, AmbiguousResolution)
    assert result.outcome == "SWEPT_ON_TIMEOUT"
    # The timeout fallback is a SWEEP, not a retry: cancel-all fired exactly once and submits blocked.
    assert result.cancel_all_ack is not None
    assert cancel_spy.cancel_all_calls == 1
    assert session.submit_blocked is True
    assert controller.check_can_submit(session) is False
    # Bounded polling actually happened before the timeout (100ms ticks up to the 300ms bound).
    assert result.polls >= 3
    assert result.elapsed_ms >= 300
    # NO resubmit: the loop has no submit seam — only the three read surfaces were ever touched.
    assert set(adapter.queried) <= {"get_orders", "get_order", "get_fill_history"}


# --- Resolves mid-poll: poll 2 returns RESOLVED → freeze lifts cleanly, NO cancel-all, NO retry -


async def test_poll_resolves_mid_poll_lifts_freeze_no_cancel_all() -> None:
    """A mid-poll RESOLVED lifts the freeze cleanly: no cancel-all sweep, no retry, correct exit.

    The order was AMBIGUOUS on poll 1 and the FAK lands in fill history by poll 2 — a genuine
    RE-QUERY by ``venue_order_key`` flips the real reconcile to RESOLVED. Only AMBIGUOUS holds the
    freeze, so the loop exits WITHOUT sweeping and WITHOUT re-submitting.
    """
    adapter = _ScriptedFillAdapter(resolve_on_call=2, order_hash=REC.venue_order_key)
    clk = _InjectedClock()
    cancel_spy = _CancelAllSpy()
    controller = SafetyController(clock_ms=clk.clock)
    session = DustSafetySession("dust-sess-resolve")

    result = await resolve_ambiguous_submit(
        REC,
        adapter=adapter,
        controller=controller,
        session=session,
        cancel_adapter=cancel_spy,
        clock_ms=clk.clock,
        sleep=clk.sleep,
        poll_interval_ms=100,
        timeout_ms=10_000,
    )

    assert result.outcome == "RESOLVED"
    assert result.resolved_state == "RESOLVED"
    assert result.polls == 2  # AMBIGUOUS, then RESOLVED
    # Freeze lifted cleanly: NO cancel-all sweep and NO block.
    assert result.cancel_all_ack is None
    assert cancel_spy.cancel_all_calls == 0
    assert session.submit_blocked is False
    assert controller.check_can_submit(session) is True
    # The poll RE-QUERIED venue truth (it did not re-submit): the read surface was hit across ticks.
    assert adapter.queried.count("get_fill_history") == 2


# --- Resolves to DEFINITIVELY_ABSENT mid-poll: freeze lifts cleanly (E4-T6's positive absence) ---


async def test_poll_resolves_to_definitively_absent_lifts_freeze() -> None:
    """A DEFINITIVELY_ABSENT resolution also lifts the freeze cleanly — no sweep, no retry.

    Only AMBIGUOUS holds the freeze. Uses an injected poll (the positive-absence path that yields
    DEFINITIVELY_ABSENT is deferred to E4-T6; the loop treats any non-AMBIGUOUS terminal as a clean
    freeze lift).
    """
    states = iter(["AMBIGUOUS", "AMBIGUOUS", "DEFINITIVELY_ABSENT"])

    async def _poll() -> UncertainSubmitState:
        return next(states)  # type: ignore[return-value]

    clk = _InjectedClock()
    cancel_spy = _CancelAllSpy()
    controller = SafetyController(clock_ms=clk.clock)
    session = DustSafetySession("dust-sess-absent")

    result = await resolve_ambiguous_submit(
        REC,
        adapter=FakeAdapter(),
        controller=controller,
        session=session,
        cancel_adapter=cancel_spy,
        clock_ms=clk.clock,
        sleep=clk.sleep,
        poll_interval_ms=50,
        timeout_ms=10_000,
        poll_fn=_poll,
    )

    assert result.outcome == "DEFINITIVELY_ABSENT"
    assert result.polls == 3
    assert result.cancel_all_ack is None
    assert cancel_spy.cancel_all_calls == 0
    assert session.submit_blocked is False


# --- The timeout sweep carries an honest cause and re-queries (never re-submits) before it fires --


async def test_timeout_sweep_uses_injected_cause_and_stays_bounded() -> None:
    """The timeout fallback fires the sweep under its injected honest cause; polling stays bounded."""
    adapter = FakeAdapter(open_orders=[], fill_history=[], status_by_id=None)  # always AMBIGUOUS
    clk = _InjectedClock()
    cancel_spy = _CancelAllSpy(canceled=1)
    controller = SafetyController(clock_ms=clk.clock)
    session = DustSafetySession("dust-sess-cause")

    result = await resolve_ambiguous_submit(
        REC,
        adapter=adapter,
        controller=controller,
        session=session,
        cancel_adapter=cancel_spy,
        clock_ms=clk.clock,
        sleep=clk.sleep,
        poll_interval_ms=50,
        timeout_ms=100,
        timeout_cause="manual",
    )

    assert result.outcome == "SWEPT_ON_TIMEOUT"
    assert result.cancel_all_ack is not None
    assert result.cancel_all_ack.trigger_cause == "manual"
    assert session.block_cause == "manual"
    # Bounded: it did not spin forever — a small, finite number of polls before the sweep.
    assert 1 <= result.polls <= 5
