"""E3-T2 tests — hashed, fail-closed per-market fee snapshot + venue-precision round5 (IDM-005/DAT-004).

TRUST-CRITICAL (MONEY-NETWORK BOUNDARY, Mode-A FAKE only; Mode B UNARMED). The fee snapshot is a
load-bearing input to the fee-inclusive realized-loss risk path (``risk.RealizedFillRecord.net_pnl``),
so its guarantees are proven here against a FAKE ``get_market`` client (no network, no credentials):

* HASHED + PINNED-ONCE: ``pin_fee_snapshot`` reads the per-market fee descriptor from ``get_market``
  EXACTLY ONCE and returns a frozen, hashed snapshot. Later fee computations use the PINNED fields,
  never a fresh venue call — proven by asserting ``get_market`` was called once and no more.
* FAIL-CLOSED IF UNAVAILABLE: a missing fee descriptor, an error from ``get_market``, or a
  non-finite/negative rate raises :class:`FailClosed` — NEVER a silent ``0`` or a guessed default.
* round5 (venue precision, E3-T0 §8): 5 dp; computed fee ``< 0.00001`` -> ``0`` (not charged); the
  smallest nonzero is ``0.00001``; NO upward floor (a sub-0.00001 fee drops to 0, it is not bumped up).
* TAKER FEE = ``round5(feeRate·shares·p·(1−p))``, peak at ``p=0.5``; MAKER rung fee = ``0`` SOURCED
  from the snapshot's taker-only fee model (``fd.to``), never a hardcoded call-site literal.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from pydantic import ValidationError

from veridex.dust_execution.feesnapshot import FeeSnapshot, pin_fee_snapshot, round5
from veridex.dust_execution.risk import FailClosed

# ---------------------------------------------------------------------------
# FAKE get_market client — emulates the E3-T0 §8/§10 getClobMarketInfo shape.
# ---------------------------------------------------------------------------


class _FakeMarketClient:
    """Fake CLOB market-info client returning the pinned §8 ``fd`` fee-descriptor shape.

    ``fee_rate=None`` omits ``fd`` entirely (fee params unavailable -> the snapshot must fail
    closed). ``get_market_calls`` counts wire reads so the pinned-once invariant is testable.
    """

    def __init__(
        self, *, fee_rate: float | None, exponent: int = 1, taker_only: bool = True
    ) -> None:
        self._fd = None if fee_rate is None else {"r": fee_rate, "e": exponent, "to": taker_only}
        self.get_market_calls = 0

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        self.get_market_calls += 1
        info: dict[str, Any] = {
            "condition_id": condition_id,
            "t": [{"t": "111", "o": "yes"}, {"t": "222", "o": "no"}],
            "mts": 0.01,
            "nr": False,
        }
        if self._fd is not None:
            info["fd"] = self._fd
        return info


class _ErrorMarketClient:
    """get_market raises — the venue is unavailable, so the snapshot must fail closed."""

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        raise RuntimeError("venue unavailable")


# ---------------------------------------------------------------------------
# The RED anchor: hashed snapshot + round5 boundary (IDM-005/DAT-004, AC-040).
# ---------------------------------------------------------------------------


async def test_fee_snapshot_hashed_and_round5_boundary() -> None:
    # --- taker fee peaks at p=0.5 (symmetric feeRate·p·(1−p)) ------------------------------
    snap = await pin_fee_snapshot(_FakeMarketClient(fee_rate=0.05), "0xcond")
    peak = snap.taker_fee(shares=1.0, price=0.5)
    assert peak == round5(0.05 * 0.5 * 0.5) == 0.0125
    # Peak property: the symmetric fee is <= the p=0.5 peak for any other price.
    assert snap.taker_fee(shares=1.0, price=0.3) < peak
    assert snap.taker_fee(shares=1.0, price=0.7) < peak
    assert snap.taker_fee(shares=1.0, price=0.3) == snap.taker_fee(shares=1.0, price=0.7)

    # --- boundary: computed fee < 0.00001 -> 0 (not charged); NO upward floor -------------
    tiny = await pin_fee_snapshot(_FakeMarketClient(fee_rate=0.000016), "0xcond")
    # raw = 0.000016 * 1 * 0.25 = 0.000004  (< 0.00001)  ->  0.0, NOT bumped up to 0.00001.
    assert tiny.taker_fee(shares=1.0, price=0.5) == 0.0

    # --- boundary: exactly 0.00001 (the smallest nonzero) --------------------------------
    smallest = await pin_fee_snapshot(_FakeMarketClient(fee_rate=0.00004), "0xcond")
    # raw = 0.00004 * 1 * 0.25 = 0.00001 exactly -> stays 0.00001 (not zeroed).
    assert smallest.taker_fee(shares=1.0, price=0.5) == 0.00001

    # --- round5 helper: nearest (not floor); sub-threshold -> 0 --------------------------
    assert round5(0.000004) == 0.0  # below threshold -> zero
    assert round5(0.00001) == 0.00001  # smallest nonzero, unchanged
    assert round5(0.000006) == 0.00001  # nearest rounds UP a >=half value; not an upward floor

    # --- snapshot unavailable -> fail closed (never a silent 0 / guessed default) --------
    with pytest.raises(FailClosed):
        await pin_fee_snapshot(_FakeMarketClient(fee_rate=None), "0xcond")  # no fd descriptor
    with pytest.raises(FailClosed):
        await pin_fee_snapshot(_ErrorMarketClient(), "0xcond")  # get_market errored

    # --- maker rung fee = 0 SOURCED from the snapshot's taker-only model (not hardcoded) --
    assert snap.taker_only is True
    assert snap.maker_fee(shares=1.0, price=0.5) == 0.0

    # --- hashed + deterministic: equal fee params -> equal hash; different -> different ---
    snap_again = await pin_fee_snapshot(_FakeMarketClient(fee_rate=0.05), "0xcond")
    assert snap.snapshot_hash == snap_again.snapshot_hash
    assert snap.snapshot_hash != tiny.snapshot_hash
    assert re.fullmatch(r"[0-9a-f]{64}", snap.snapshot_hash)


# ---------------------------------------------------------------------------
# Pinned-once: fee computations never trigger a fresh venue read.
# ---------------------------------------------------------------------------


async def test_snapshot_pinned_once_no_refetch_on_use() -> None:
    client = _FakeMarketClient(fee_rate=0.05)
    snap = await pin_fee_snapshot(client, "0xcond")
    assert client.get_market_calls == 1

    snap.taker_fee(shares=3.0, price=0.5)
    snap.maker_fee(shares=3.0, price=0.5)
    _ = snap.snapshot_hash

    assert client.get_market_calls == 1, "using the pinned snapshot must not re-call get_market"


# ---------------------------------------------------------------------------
# fd.e / fd.to are captured and participate in the pinned hash (audit).
# ---------------------------------------------------------------------------


async def test_fee_exponent_and_taker_only_pinned_and_hashed() -> None:
    snap = await pin_fee_snapshot(
        _FakeMarketClient(fee_rate=0.05, exponent=1, taker_only=True), "0xcond"
    )
    assert snap.fee_rate == 0.05
    assert snap.fee_exponent == 1
    assert snap.taker_only is True

    # A different exponent yields a different pinned hash (exponent is part of the frozen record).
    snap_e2 = await pin_fee_snapshot(
        _FakeMarketClient(fee_rate=0.05, exponent=2, taker_only=True), "0xcond"
    )
    assert snap.snapshot_hash != snap_e2.snapshot_hash


# ---------------------------------------------------------------------------
# Fail-closed on a malformed fee rate (never guess a fund-touching number).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_rate", [-0.01, float("nan"), float("inf")])
async def test_snapshot_fails_closed_on_bad_rate(bad_rate: float) -> None:
    with pytest.raises(FailClosed):
        await pin_fee_snapshot(_FakeMarketClient(fee_rate=bad_rate), "0xcond")


# ---------------------------------------------------------------------------
# Snapshot is frozen (a fee param cannot be mutated after pinning).
# ---------------------------------------------------------------------------


async def test_snapshot_is_frozen() -> None:
    snap = await pin_fee_snapshot(_FakeMarketClient(fee_rate=0.05), "0xcond")
    assert isinstance(snap, FeeSnapshot)
    with pytest.raises(ValidationError):  # pydantic frozen -> ValidationError on assignment
        snap.fee_rate = 0.99  # type: ignore[misc]
