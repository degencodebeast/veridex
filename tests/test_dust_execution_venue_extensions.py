"""E3-T1/T2 tests — signer-neutral write seam + additive venue read/reconciliation surface.

E3-T1 (SAF-008, PAT-002, §6 groups 7/11): signer-neutral write seam, fail-closed unarmed.
E3-T2 (IDM-005, DAT-004, §6 groups 3/17): expose the vendored ``get_orders``/``get_order``/
``get_market`` reads plus a NET-NEW ``get_fill_history`` surface on the ``WriteClient`` Protocol +
``PolymarketAdapter`` — ADDITIVE (the sealed four-method ``VenueAdapter`` contract is untouched),
tested against the Mode-A FAKE recon client (no network, no signing; Mode B UNARMED).

TRUST-CRITICAL (MONEY-NETWORK BOUNDARY). This lane's write seam is the ONLY thing that could ever
reach a real-money venue wire, so the load-bearing guarantees are proven here with INJECTED
recording fakes and ZERO network / ZERO real signing:

* FAIL-CLOSED = DEFAULT-DENY (SAF-008): a real-money submit is armed ONLY when ALL THREE conditions
  hold together — ``polymarket_write_enabled`` true AND ``dry_run`` false AND an injected write
  client present (mirrors ``PolymarketAdapter._require_armed``, polymarket.py:472-511, which this
  seam CONSUMES). Missing ANY ONE → the seam REFUSES by raising BEFORE any I/O.

* REFUSE-BEFORE-I/O (recording-fake teeth): the seam arms FIRST, so when unarmed neither the signer
  NOR the venue write client is ever touched. The tests assert the recording fakes' methods were
  NEVER called — a state-only "returned False" is not enough. Bypassing the arm gate (force-armed)
  makes an unarmed submit REACH the fake wire, which flips these assertions to failure (mutation).

* PROVIDER-NEUTRAL SIGNER (PAT-002): ``WalletControlPlane``/``Signer`` is a Protocol; this task
  supplies the Mode-A ``LocalFakeWalletControlPlane`` (deterministic, non-secret, offline — never a
  real key, never real signing). The Mode-B ``PrivyEvmWalletControlPlane`` (E3-T7) implements the
  same Protocol later, so nothing here hardcodes Privy or a specific wire shape.

* PROVIDER ERRORS FAIL CLOSED: if signing raises, the seam never proceeds to the wire.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from veridex.config import Settings
from veridex.dust_execution.risk import FailClosed
from veridex.dust_execution.signer import (
    ArmingInputs,
    LocalFakeWalletControlPlane,
    SignedArtifact,
    Signer,
    SignerBackedWriteSeam,
    SigningPayload,
    WalletControlPlane,
    require_armed,
)
from veridex.venues.base import VenueAdapter, VenueReconciliationReads
from veridex.venues.polymarket import PolymarketAdapter, PolymarketWriteDisabled
from veridex.venues.polymarket_resolver import ResolvedMarket

# ---------------------------------------------------------------------------
# Fixtures / recording fakes (no network, no signing)
# ---------------------------------------------------------------------------

_RESOLVED = ResolvedMarket(
    condition_id="0xcond",
    token_id_yes="111",
    token_id_no="222",
    tick_size=0.01,
)


def _payload() -> SigningPayload:
    return SigningPayload(
        token_id="111",
        side="BUY",
        native_price=0.42,
        size=1.0,
        tif="FAK",
        tick_size="0.01",
        client_order_id="coid-1",
    )


class _FakeBookClient:
    """Read-path book client — required by the adapter constructor, unused by the write seam."""

    async def get_book(self, token_id: str) -> dict[str, Any]:
        return {"bids": [], "asks": [], "timestamp": 0}


class _RecordingWriteClient:
    """Recording fake for the venue WRITE wire — CAPTURES calls, no network, no signing.

    ``limit_order`` appends to ``calls`` and never touches a network. The tests assert this list
    stays EMPTY when unarmed (refuse-before-I/O), so a soft "returned False" cannot pass.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def limit_order(
        self,
        ticker: str,
        amount: float,
        price: float,
        tif: str = "GTC",
        round_price: bool = True,
        tick_size: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            {"ticker": ticker, "amount": amount, "price": price, "tif": tif}
        )
        return {"success": True, "orderID": "0xabc"}


class _RecordingSigner:
    """Recording fake implementing ``WalletControlPlane`` — CAPTURES sign calls, no real signing.

    Asserted NEVER-called on the unarmed path (the seam arms before it signs).
    """

    mode = "FAKE_LOCAL"

    def __init__(self) -> None:
        self.sign_calls: list[SigningPayload] = []

    async def sign_order(self, payload: SigningPayload) -> SignedArtifact:
        self.sign_calls.append(payload)
        return SignedArtifact(mode="FAKE_LOCAL", signature="fakesig:deadbeef", order_digest="deadbeef")


class _ExplodingSigner:
    """Signer whose provider errors — proves provider errors fail closed (never reach the wire)."""

    mode = "FAKE_LOCAL"

    async def sign_order(self, payload: SigningPayload) -> SignedArtifact:
        raise RuntimeError("signing provider unavailable")


def _unarmed_adapter(
    write_client: _RecordingWriteClient,
    *,
    write_enabled: bool,
    dry_run: bool,
) -> PolymarketAdapter:
    """A real ``PolymarketAdapter`` whose ``_require_armed`` the seam CONSUMES (polymarket.py:472-511)."""
    return PolymarketAdapter(
        _RESOLVED,
        _FakeBookClient(),
        settings=Settings(_env_file=None, polymarket_write_enabled=write_enabled),
        write_client=write_client,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# (1) Unarmed submit REFUSES before any I/O — recording-fake teeth (SAF-008)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("write_enabled", "dry_run"),
    [
        (False, False),  # write not enabled -> refuse
        (True, True),  # write enabled but DRY_RUN (safe default) -> refuse
        (False, True),  # neither -> refuse
    ],
)
async def test_unarmed_submit_refuses_before_any_io(write_enabled: bool, dry_run: bool) -> None:
    write = _RecordingWriteClient()
    signer = _RecordingSigner()
    adapter = _unarmed_adapter(write, write_enabled=write_enabled, dry_run=dry_run)
    # The seam CONSUMES the adapter's real all-three arming gate (polymarket.py:472-511).
    seam = SignerBackedWriteSeam(arm=adapter._require_armed, signer=signer)

    with pytest.raises(PolymarketWriteDisabled):
        await seam.submit(_payload())

    # REFUSE-BEFORE-I/O: neither the venue wire NOR the signer was ever touched.
    assert write.calls == [], "unarmed submit reached the venue write wire"
    assert signer.sign_calls == [], "unarmed submit reached the signer"


async def test_unarmed_submit_refuses_when_write_client_missing() -> None:
    signer = _RecordingSigner()
    # write_enabled + not dry_run, but NO write client injected -> still unarmed (third condition).
    adapter = PolymarketAdapter(
        _RESOLVED,
        _FakeBookClient(),
        settings=Settings(_env_file=None, polymarket_write_enabled=True),
        write_client=None,
        dry_run=False,
    )
    seam = SignerBackedWriteSeam(arm=adapter._require_armed, signer=signer)

    with pytest.raises(PolymarketWriteDisabled):
        await seam.submit(_payload())
    assert signer.sign_calls == [], "unarmed (no client) submit reached the signer"


# ---------------------------------------------------------------------------
# (2) Provider-neutral fail-closed gate — ALL THREE required (default-deny)
# ---------------------------------------------------------------------------


def test_require_armed_all_three_returns_client() -> None:
    write = _RecordingWriteClient()
    inputs = ArmingInputs(write_enabled=True, dry_run=False, write_client=write)
    assert require_armed(inputs, action="submit") is write


@pytest.mark.parametrize(
    "inputs",
    [
        ArmingInputs(write_enabled=False, dry_run=False, write_client=object()),
        ArmingInputs(write_enabled=True, dry_run=True, write_client=object()),
        ArmingInputs(write_enabled=True, dry_run=False, write_client=None),
    ],
)
def test_require_armed_missing_any_condition_fails_closed(inputs: ArmingInputs) -> None:
    with pytest.raises(FailClosed):
        require_armed(inputs, action="submit")


async def test_seam_driven_by_provider_neutral_gate_refuses_before_io() -> None:
    write = _RecordingWriteClient()
    signer = _RecordingSigner()
    inputs = ArmingInputs(write_enabled=True, dry_run=True, write_client=write)  # dry_run -> unarmed
    seam = SignerBackedWriteSeam(arm=inputs.arm_gate(), signer=signer)

    with pytest.raises(FailClosed):
        await seam.submit(_payload())
    assert write.calls == []
    assert signer.sign_calls == []


# ---------------------------------------------------------------------------
# (3) Armed happy path — proves the wire IS reachable when armed (mutation baseline)
# ---------------------------------------------------------------------------


async def test_armed_submit_signs_then_writes_once() -> None:
    write = _RecordingWriteClient()
    signer = _RecordingSigner()
    inputs = ArmingInputs(write_enabled=True, dry_run=False, write_client=write)
    seam = SignerBackedWriteSeam(arm=inputs.arm_gate(), signer=signer)

    await seam.submit(_payload())

    assert len(signer.sign_calls) == 1, "armed submit must sign exactly once"
    assert len(write.calls) == 1, "armed submit must reach the wire exactly once"
    # The native (tick-unit) price crosses the wire, never a decimal-odds value.
    assert write.calls[0]["price"] == 0.42


# ---------------------------------------------------------------------------
# (4) Provider errors fail closed — a signing error never reaches the wire
# ---------------------------------------------------------------------------


async def test_signer_provider_error_fails_closed_before_wire() -> None:
    write = _RecordingWriteClient()
    inputs = ArmingInputs(write_enabled=True, dry_run=False, write_client=write)
    seam = SignerBackedWriteSeam(arm=inputs.arm_gate(), signer=_ExplodingSigner())

    with pytest.raises(RuntimeError):
        await seam.submit(_payload())
    assert write.calls == [], "a signing provider error must not reach the venue wire"


# ---------------------------------------------------------------------------
# (5) Mode-A fake signer — deterministic, non-secret, provider-neutral Protocol
# ---------------------------------------------------------------------------


async def test_mode_a_fake_signer_is_deterministic_and_non_secret() -> None:
    signer = LocalFakeWalletControlPlane()
    a = await signer.sign_order(_payload())
    b = await signer.sign_order(_payload())

    assert isinstance(a, SignedArtifact)
    assert a.signature == b.signature, "Mode-A fake must be deterministic (offline replayable)"
    assert a.order_digest == b.order_digest
    assert signer.mode == "FAKE_LOCAL"
    # Non-secret: the artifact is an opaque digest, never key material.
    assert re.fullmatch(r"[0-9a-f]{64}", a.order_digest), "digest must be a hex sha256"
    assert "0x" not in a.signature.lower().replace("fakesig", "")  # no address/key smuggled


def test_fake_signer_satisfies_the_provider_neutral_protocol() -> None:
    signer = LocalFakeWalletControlPlane()
    assert isinstance(signer, WalletControlPlane)
    # ``Signer`` is the public alias of the same provider-neutral Protocol.
    assert Signer is WalletControlPlane


# ===========================================================================
# E3-T2 — additive venue read / reconciliation surface (IDM-005, DAT-004).
# ===========================================================================
#
# expose the vendored get_orders(**kwargs) (paginated) / get_order(order_id) /
# get_market(condition_id) reads + a NET-NEW get_fill_history surface (§3 get_trades shape) on the
# WriteClient Protocol AND PolymarketAdapter. These are READ/reconciliation surfaces (own-order
# status + own-fill history + public fee info), tested against a Mode-A FAKE that emulates the
# E3-T0 §3/§5/§8 pinned shapes. ADDITIVE: the sealed four-method VenueAdapter contract is untouched.
# (VenueAdapter + VenueReconciliationReads are imported at the top of this module.)


class _ReconWriteClient:
    """Mode-A FAKE CLOB write+recon client — emulates the E3-T0 §3/§5/§8 pinned shapes, no network.

    ``get_order``/``get_orders`` return the §5 OpenOrder EXACT-SET shape; ``get_market`` the §8
    ``fd`` fee descriptor; ``get_fill_history`` the §3 Trade EXACT-SET shape keyed by the local
    ``taker_order_id`` (orderHash). Every call is recorded so delegation is provable.
    """

    def __init__(self) -> None:
        self.get_order_calls: list[str] = []
        self.get_orders_calls: list[dict[str, Any]] = []
        self.get_market_calls: list[str] = []
        self.fill_calls: list[dict[str, Any]] = []

    async def limit_order(
        self,
        ticker: str,
        amount: float,
        price: float,
        tif: str = "GTC",
        round_price: bool = True,
        tick_size: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {"success": True, "orderID": "0xabc"}

    def _open_order(self, order_id: str = "0xhash") -> dict[str, Any]:
        return {  # §5 OpenOrder EXACT SET
            "id": order_id,
            "status": "LIVE",
            "market": "0xcond",
            "asset_id": "111",
            "side": "BUY",
            "original_size": "10",
            "size_matched": "4",
            "price": "0.42",
            "outcome": "YES",
            "order_type": "GTC",
            "maker_address": "0xmaker",
            "owner": "api-key-uuid",
            "expiration": "0",
            "associate_trades": [],
            "created_at": 1710000000,
        }

    async def get_order(self, order_id: str, **kwargs: Any) -> dict[str, Any]:
        self.get_order_calls.append(order_id)
        return self._open_order(order_id)

    async def get_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
        # Vendored get_orders(**kwargs) is paginated and returns a FLATTENED list of open orders.
        self.get_orders_calls.append(kwargs)
        return [self._open_order("0xhash"), self._open_order("0xhash2")]

    async def get_market(self, condition_id: str, **kwargs: Any) -> dict[str, Any]:
        self.get_market_calls.append(condition_id)
        return {  # §8 getClobMarketInfo EXACT SET
            "condition_id": condition_id,
            "t": [{"t": "111", "o": "yes"}, {"t": "222", "o": "no"}],
            "mts": 0.01,
            "nr": False,
            "fd": {"r": 0.05, "e": 1, "to": True},
        }

    async def get_fill_history(self, **kwargs: Any) -> list[dict[str, Any]]:
        # NET-NEW surface (no vendored endpoint, G9) — §3 Trade EXACT SET keyed by orderHash.
        self.fill_calls.append(kwargs)
        return [
            {
                "id": "t1",
                "taker_order_id": "0xhash",
                "market": "0xcond",
                "asset_id": "111",
                "side": "BUY",
                "size": "4",
                "price": "0.42",
                "fee_rate_bps": "50",
                "status": "CONFIRMED",
                "match_time": 1710000000,
                "last_update": 1710000001,
                "outcome": "YES",
                "owner": "api-key-uuid",
                "maker_address": "0xmaker",
                "trader_side": "TAKER",
                "transaction_hash": "0xtx",
                "bucket_index": 0,
                "maker_orders": [],
            }
        ]


def _recon_adapter(
    client: _ReconWriteClient,
    *,
    write_enabled: bool = True,
) -> PolymarketAdapter:
    return PolymarketAdapter(
        _RESOLVED,
        _FakeBookClient(),
        settings=Settings(_env_file=None, polymarket_write_enabled=write_enabled),
        write_client=client,
        dry_run=True,  # reads never touch money -> gated on write_enabled only, like get_order_status
    )


# --- Protocol shape: the new reconciliation read surface lives in base.py (additive) --------


def test_recon_reads_protocol_is_satisfied_by_the_fake_and_the_adapter() -> None:
    client = _ReconWriteClient()
    adapter = _recon_adapter(client)
    # The FAKE and the real adapter both structurally satisfy the NEW additive read Protocol.
    assert isinstance(client, VenueReconciliationReads)
    assert isinstance(adapter, VenueReconciliationReads)
    # A bare object without the read methods does NOT satisfy it (the Protocol has teeth).
    assert not isinstance(object(), VenueReconciliationReads)


def test_adapter_still_satisfies_sealed_venue_adapter_contract() -> None:
    # ADDITIVE guarantee: exposing the read surface does not break VenueAdapter conformance.
    adapter = _recon_adapter(_ReconWriteClient())
    assert isinstance(adapter, VenueAdapter)


# --- Delegation: the adapter exposes each vendored read, gated behind the write flag ---------


async def test_get_order_exposes_raw_open_order_record() -> None:
    client = _ReconWriteClient()
    adapter = _recon_adapter(client)
    rec = await adapter.get_order("0xhash")
    assert client.get_order_calls == ["0xhash"]
    # §5 OpenOrder EXACT SET keys surfaced verbatim (raw record for E4 reconciliation).
    assert rec["id"] == "0xhash"
    assert rec["size_matched"] == "4"
    assert rec["associate_trades"] == []


async def test_get_orders_returns_paginated_flattened_open_orders() -> None:
    client = _ReconWriteClient()
    adapter = _recon_adapter(client)
    orders = await adapter.get_orders(market="0xcond", asset_id="111")
    assert client.get_orders_calls == [{"market": "0xcond", "asset_id": "111"}]
    assert isinstance(orders, list)
    assert [o["id"] for o in orders] == ["0xhash", "0xhash2"]


async def test_get_market_returns_fee_descriptor_shape() -> None:
    client = _ReconWriteClient()
    adapter = _recon_adapter(client)
    info = await adapter.get_market("0xcond")
    assert client.get_market_calls == ["0xcond"]
    assert info["fd"] == {"r": 0.05, "e": 1, "to": True}


async def test_get_fill_history_returns_trade_shape_keyed_by_order_hash() -> None:
    client = _ReconWriteClient()
    adapter = _recon_adapter(client)
    fills = await adapter.get_fill_history(market="0xcond")
    assert client.fill_calls == [{"market": "0xcond"}]
    assert isinstance(fills, list)
    # §3 Trade join key: taker_order_id == the local orderHash we submitted with (E4 reconciliation).
    assert fills[0]["taker_order_id"] == "0xhash"
    assert fills[0]["status"] == "CONFIRMED"  # §3c terminal
    assert fills[0]["bucket_index"] == 0


# --- Fail-closed gating: own-order/own-fill reads need write-enabled + a client --------------


@pytest.mark.parametrize(
    "read",
    ["get_orders", "get_order", "get_fill_history"],
)
async def test_recon_reads_fail_closed_when_write_disabled(read: str) -> None:
    client = _ReconWriteClient()
    adapter = _recon_adapter(client, write_enabled=False)
    method = getattr(adapter, read)
    with pytest.raises(PolymarketWriteDisabled):
        if read == "get_order":
            await method("0xhash")
        else:
            await method()


async def test_recon_reads_fail_closed_when_no_write_client() -> None:
    adapter = PolymarketAdapter(
        _RESOLVED,
        _FakeBookClient(),
        settings=Settings(_env_file=None, polymarket_write_enabled=True),
        write_client=None,
        dry_run=True,
    )
    with pytest.raises(PolymarketWriteDisabled):
        await adapter.get_orders()


async def test_get_market_is_public_but_needs_a_client() -> None:
    # Fee info is a PUBLIC data endpoint (auth: None), so it does NOT require write-enabled; it does
    # require an injected client to reach the wire (fail-closed when the live path is not wired).
    client = _ReconWriteClient()
    adapter = _recon_adapter(client, write_enabled=False)
    info = await adapter.get_market("0xcond")
    assert info["condition_id"] == "0xcond"

    no_client = PolymarketAdapter(
        _RESOLVED,
        _FakeBookClient(),
        settings=Settings(_env_file=None, polymarket_write_enabled=True),
        write_client=None,
        dry_run=True,
    )
    with pytest.raises(PolymarketWriteDisabled):
        await no_client.get_market("0xcond")


# ===========================================================================
# E3-T3 — distinct GTC/GTD post-only resting-order contract (REQ-016, AC-031, §6 grp 16).
# ===========================================================================
#
# CONTRACT DECISION (resolves Fable-M1 + REQ-016): the directional taker
# ``veridex.venues.base.Order`` stays FAK/FOK-only and its GTC-ban test stays GREEN. R4-A introduces
# a PHYSICALLY DISTINCT resting-order contract (``veridex.dust_execution.resting_order.RestingOrder``,
# ``tif: Literal["GTC","GTD"]``, rests on the book) in its OWN lane. FAK/FOK never rest; the two order
# types are separate Python types — a maker demo cannot submit a taker order and a directional taker
# can never emit GTC.
#
# Built against the E3-T0 §6 pinned resting-maker wire:
#   * post-only field: top-level ``postOnly`` (bool), ALO semantic; valid ONLY with GTC/GTD.
#   * GTC/GTD: ``orderType`` string enum ``"GTC"``/``"GTD"``.
#   * GTD expiration: ``order.expiration`` (unix SECONDS), NOT signed; must be >= 3 min in the future.
# Tested against the Mode-A FAKE resting venue (no network, no signing; Mode B UNARMED).

from pydantic import ValidationError  # noqa: E402

from veridex.venues.base import Order as _TakerOrder  # noqa: E402


class _RestingVenueClient:
    """Mode-A FAKE CLOB resting-venue client — a resting order RESTS; a FAK order NEVER rests.

    ``submit_resting_order`` (the §6 resting wire) adds the order to the open-order book and returns a
    ``status:"live"`` SendOrderResponse (§2c). ``limit_order`` is the taker FAK path — it fills-or-kills
    and is NEVER added to the resting book. ``get_orders`` returns the resting open orders (§5), so the
    "rested vs never-rested" distinction is observable exactly as E4 reconciliation will see it.
    """

    def __init__(self, now: int = 1_000_000) -> None:
        self._now = now
        self._resting: list[dict[str, Any]] = []
        self.submit_resting_calls: list[dict[str, Any]] = []
        self.fak_calls: list[dict[str, Any]] = []

    async def submit_resting_order(
        self,
        *,
        token_id: str,
        amount: float,
        native_price: float,
        order_type: str,
        post_only: bool,
        expiration: int,
        tick_size: str | None = None,
    ) -> dict[str, Any]:
        self.submit_resting_calls.append(
            {
                "token_id": token_id,
                "amount": amount,
                "native_price": native_price,
                "order_type": order_type,
                "post_only": post_only,
                "expiration": expiration,
            }
        )
        # §6 GTD temporal rule: expiration must be >= 3 minutes in the future, else INVALID_ORDER_EXPIRATION.
        if order_type == "GTD" and expiration < self._now + 180:
            return {"success": False, "orderID": "", "status": "unmatched",
                    "errorMsg": "INVALID_ORDER_EXPIRATION"}
        order_id = f"0xresting{len(self._resting)}"
        self._resting.append(
            {  # §5 OpenOrder EXACT SET
                "id": order_id,
                "status": "LIVE",
                "market": "0xcond",
                "asset_id": token_id,
                "side": "BUY" if amount > 0 else "SELL",
                "original_size": str(abs(amount)),
                "size_matched": "0",
                "price": str(native_price),
                "outcome": "YES",
                "order_type": order_type,
                "maker_address": "0xmaker",
                "owner": "api-key-uuid",
                "expiration": str(expiration),
                "associate_trades": [],
                "created_at": self._now,
            }
        )
        return {"success": True, "orderID": order_id, "status": "live", "errorMsg": ""}

    async def limit_order(
        self,
        ticker: str,
        amount: float,
        price: float,
        tif: str = "GTC",
        round_price: bool = True,
        tick_size: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Taker FAK path: fill-and-kill. It NEVER rests — the resting book is untouched.
        self.fak_calls.append({"ticker": ticker, "amount": amount, "tif": tif})
        return {"success": True, "orderID": "0xfak", "status": "matched"}

    async def get_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self._resting)


def _resting_adapter(client: _RestingVenueClient, *, armed: bool = True) -> PolymarketAdapter:
    return PolymarketAdapter(
        _RESOLVED,
        _FakeBookClient(),
        settings=Settings(_env_file=None, polymarket_write_enabled=armed),
        write_client=client,
        dry_run=not armed,
    )


# --- Step 1 keep-green guard (FIRST): the sealed taker contract is UNTOUCHED -----------------


def test_directional_order_still_bans_gtc() -> None:
    # KEEP-GREEN GUARD (must pass at ALL times, incl. RED): R4-A did NOT touch the sealed taker
    # ``Order`` — its ``tif`` Literal still excludes GTC, so ``Order(tif="GTC")`` still fails.
    # (No dependency on the not-yet-existing resting primitive → green even before GREEN.)
    with pytest.raises(ValidationError):
        _TakerOrder(
            market_ref="OU|2.5|full",
            side="over",
            size=100.0,
            price=2.0,
            venue="polymarket",
            client_order_id="c1",
            tif="GTC",  # type: ignore[arg-type]
        )


# --- The distinct RestingOrder rests; the taker FAK order never rests ------------------------


async def test_resting_order_rests_and_appears_in_get_orders_fak_never_rests() -> None:
    # Local import so RED = ImportError HERE (Mode B maker admission blocked until the primitive
    # exists), while the keep-green guard above still passes.
    from veridex.dust_execution.resting_order import (
        RestingOrder,
        maker_mode_b_resting_primitive_ready,
        submit_resting_order,
    )

    # Mode B maker admission is UNBLOCKED only once this primitive is present.
    assert maker_mode_b_resting_primitive_ready() is True

    client = _RestingVenueClient()
    resting = RestingOrder(
        token_id="111",
        side="BUY",
        size=5.0,
        native_price=0.42,
        tick_size=0.01,
        tif="GTC",
        post_only=True,
        client_order_id="coid-rest-1",
    )
    ack = await submit_resting_order(resting, client=client)
    assert ack.accepted is True
    assert ack.venue_order_id  # a resting order id came back

    # The GTC post-only order RESTS and appears in get_orders (§5 open-order book).
    open_orders = await client.get_orders()
    assert [o["order_type"] for o in open_orders] == ["GTC"]
    assert open_orders[0]["asset_id"] == "111"
    assert open_orders[0]["status"] == "LIVE"

    # A taker FAK order NEVER rests: submitting it does not add to the resting book.
    taker = _TakerOrder(
        market_ref="OU|2.5|full", side="over", size=5.0, price=2.05,
        venue="polymarket", client_order_id="coid-fak-1", tif="FAK",
    )
    await client.limit_order(ticker="111", amount=taker.size, price=0.49, tif=taker.tif)
    still_only_resting = await client.get_orders()
    assert len(still_only_resting) == 1, "a FAK order must NEVER rest in the open-order book"
    assert client.fak_calls[0]["tif"] == "FAK"


def test_resting_order_is_physically_distinct_from_taker_order() -> None:
    from veridex.dust_execution.resting_order import RestingOrder

    resting = RestingOrder(
        token_id="111", side="BUY", size=5.0, native_price=0.42, tick_size=0.01,
        tif="GTC", client_order_id="c1",
    )
    # Two separate Python types: a RestingOrder is NOT a taker Order and vice-versa.
    assert not isinstance(resting, _TakerOrder)
    # The taker Order cannot represent GTC/GTD (FAK/FOK only); the RestingOrder cannot represent
    # FAK/FOK (GTC/GTD only). The overload REQ-016 forbids is impossible by construction.
    with pytest.raises(ValidationError):
        _TakerOrder(market_ref="m", side="over", size=1.0, price=2.0, venue="polymarket",
                    client_order_id="c", tif="GTD")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        RestingOrder(token_id="111", side="BUY", size=1.0, native_price=0.42, tick_size=0.01,
                     tif="FAK", client_order_id="c")  # type: ignore[arg-type]


def test_post_only_is_a_real_wire_contract_not_a_decorative_flag() -> None:
    from veridex.dust_execution.resting_order import RestingOrder

    # post_only maps to the §6 top-level ``post_only`` wire field; tif maps to ``order_type``;
    # a GTD expiration maps to the ``expiration`` (unix seconds) wire field. Real wire, not decoration.
    gtd = RestingOrder(
        token_id="111", side="SELL", size=3.0, native_price=0.30, tick_size=0.01,
        tif="GTD", post_only=True, gtd_expiration_ts=1_000_500, client_order_id="c-gtd",
    )
    kw = gtd.to_wire_kwargs()
    assert kw["order_type"] == "GTD"
    assert kw["post_only"] is True
    assert kw["expiration"] == 1_000_500
    assert kw["native_price"] == 0.30
    assert kw["amount"] == -3.0  # SELL => negative signed amount


def test_gtd_requires_expiration_and_gtc_forbids_it() -> None:
    from veridex.dust_execution.resting_order import RestingOrder

    # GTD without an expiration is incoherent -> fail closed.
    with pytest.raises(ValidationError):
        RestingOrder(token_id="111", side="BUY", size=1.0, native_price=0.42, tick_size=0.01,
                     tif="GTD", client_order_id="c")
    # GTC carrying an expiration is incoherent -> fail closed.
    with pytest.raises(ValidationError):
        RestingOrder(token_id="111", side="BUY", size=1.0, native_price=0.42, tick_size=0.01,
                     tif="GTC", gtd_expiration_ts=1_000_500, client_order_id="c")


def test_resting_order_native_tick_and_size_validation() -> None:
    from veridex.dust_execution.resting_order import RestingOrder

    # Native price must be a tick-aligned probability strictly inside (0, 1).
    with pytest.raises(ValidationError):
        RestingOrder(token_id="111", side="BUY", size=1.0, native_price=0.425, tick_size=0.01,
                     tif="GTC", client_order_id="c")  # not tick-aligned
    with pytest.raises(ValidationError):
        RestingOrder(token_id="111", side="BUY", size=1.0, native_price=1.5, tick_size=0.01,
                     tif="GTC", client_order_id="c")  # outside (0,1)
    with pytest.raises(ValidationError):
        RestingOrder(token_id="111", side="BUY", size=0.0, native_price=0.42, tick_size=0.01,
                     tif="GTC", client_order_id="c")  # non-positive size


async def test_gtd_temporal_rule_rejected_by_fake_wire_when_too_soon() -> None:
    from veridex.dust_execution.resting_order import RestingOrder, submit_resting_order

    client = _RestingVenueClient(now=1_000_000)
    too_soon = RestingOrder(
        token_id="111", side="BUY", size=1.0, native_price=0.42, tick_size=0.01,
        tif="GTD", gtd_expiration_ts=1_000_010, client_order_id="c",  # < now+180
    )
    ack = await submit_resting_order(too_soon, client=client)
    assert ack.accepted is False  # §6: INVALID_ORDER_EXPIRATION -> not rested
    assert await client.get_orders() == []


# --- The PolymarketAdapter resting-order method: armed all-three, additive -------------------


async def test_adapter_submit_resting_order_arms_and_rests() -> None:
    from veridex.dust_execution.resting_order import RestingOrder

    client = _RestingVenueClient()
    adapter = _resting_adapter(client, armed=True)  # write_enabled AND not dry_run AND client
    resting = RestingOrder(
        token_id="111", side="BUY", size=2.0, native_price=0.42, tick_size=0.01,
        tif="GTC", client_order_id="c1",
    )
    ack = await adapter.submit_resting_order(resting)
    assert ack.accepted is True
    assert len(client.submit_resting_calls) == 1
    assert (await client.get_orders())[0]["order_type"] == "GTC"


@pytest.mark.parametrize(
    ("write_enabled", "dry_run"),
    [(False, False), (True, True), (False, True)],
)
async def test_adapter_submit_resting_order_fails_closed_when_unarmed(
    write_enabled: bool, dry_run: bool
) -> None:
    from veridex.dust_execution.resting_order import RestingOrder

    client = _RestingVenueClient()
    adapter = PolymarketAdapter(
        _RESOLVED,
        _FakeBookClient(),
        settings=Settings(_env_file=None, polymarket_write_enabled=write_enabled),
        write_client=client,
        dry_run=dry_run,
    )
    resting = RestingOrder(
        token_id="111", side="BUY", size=2.0, native_price=0.42, tick_size=0.01,
        tif="GTC", client_order_id="c1",
    )
    with pytest.raises(PolymarketWriteDisabled):
        await adapter.submit_resting_order(resting)
    assert client.submit_resting_calls == [], "unarmed resting submit reached the wire"
