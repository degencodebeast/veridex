"""E3-T1 tests — signer-neutral write seam, fail-closed unarmed (SAF-008, PAT-002, §6 groups 7/11).

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
