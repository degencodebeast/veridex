"""E3-T8 — keyless L2 HMAC transport that OWNS the live commitment wiring (REQ-018d/f/f2/g).

MONEY-NETWORK BOUNDARY. This module is the LIVE submit seam, and it is STRUCTURALLY keyless of any
LOCAL SIGNING KEY: the L2 credential is an HMAC secret held in memory (not an EIP-712/secp256k1 signing
key), and the whole path imports NO local-key crypto library (``eth_account`` / ``eth_keys`` /
``coincurve`` / ``web3``). Order signing happens ONLY through the injected Privy control plane
(:class:`~veridex.dust_execution.privy_control_plane.PrivyEvmWalletControlPlane`); this transport never
holds a private key.

The crux is the LIVE submit path, whose ORDER is load-bearing and WIRED HERE (Fable-MAJOR-1 — the
commitment control lives in this transport, not a helper called elsewhere):

  (i)   COMPILE the payload (E3-T6 — done upstream; the :class:`CompiledSigningPayload` is passed in);
  (ii)  PERSIST the compound :class:`~veridex.dust_execution.contracts.PreSubmitRecord`
        ``{integrity_commitment_hash, venue_order_key (the official V2 order hash), captured_id?}`` to
        the append-only store BEFORE signing — NOT a bare digest. Persisting the venue-recognized
        ``venue_order_key`` is what lets an ACK-lost fill after a restart JOIN back to fill history
        (fill history is keyed by the official id, not Veridex's private digest) — the round-5-M2 bug
        was persisting only the private digest (v0.6.3 / Codex round-6);
  (iii) SIGN via Privy (``eth_signTypedData_v4`` through the control plane);
  (iv)  BYTE-VERIFY the EXACT outgoing POST body against the pre-sign commitment
        (:func:`verify_post_body_against_commitment`) — a covered-field mutation between commit and
        submit fails closed;
  (v)   HMAC + SEND those EXACT bytes (no re-serialization between the byte-verify and the send).

Secrets are NEVER persisted: the raw ``owner`` (L2 api-key UUID), the L2 creds, and the signature never
enter the persisted row or any log/evidence — only the one-way integrity digest and the non-secret
venue join key. SEC-005 scrubbing is extended here to the ``POLY_*`` + L2 headers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from veridex.dust_execution.contracts import PreSubmitRecord, UncertainState
from veridex.dust_execution.order_commitment import (
    OrderSigningCommitment,
    build_presubmit_record,
    verify_post_body_against_commitment,
)
from veridex.dust_execution.privy_control_plane import (
    L2ApiCredentials,
    PrivyAuthContext,
    PrivyEvmWalletControlPlane,
)
from veridex.dust_execution.risk import FailClosed
from veridex.dust_execution.signing_compiler import CompiledSigningPayload
from veridex.dust_execution.wallet_binding import ExecutionWalletBinding
from veridex.runtime.evidence import serialize_payload

#: §7b — the five L2 ``POLY_*`` headers required on every trading endpoint.
POLY_L2_HEADER_NAMES: tuple[str, ...] = (
    "POLY_ADDRESS",
    "POLY_SIGNATURE",
    "POLY_TIMESTAMP",
    "POLY_API_KEY",
    "POLY_PASSPHRASE",
)


# ---------------------------------------------------------------------------
# Append-only pre-submit store (E2-T2 append-only ledger pattern, INJECTED)
# ---------------------------------------------------------------------------


@runtime_checkable
class PreSubmitStore(Protocol):
    """Append-only store for the durable compound :class:`PreSubmitRecord` (IDM-005).

    Deliberately append-only (no update/delete) so a persisted pre-submit record can never be silently
    rewritten. The concrete store is injected; tests use :class:`InMemoryPreSubmitStore`.
    """

    def append_presubmit(self, record: PreSubmitRecord) -> None: ...

    def list_presubmit(self) -> tuple[PreSubmitRecord, ...]: ...


class InMemoryPreSubmitStore:
    """A minimal append-only in-memory :class:`PreSubmitStore` (E2-T2 ledger pattern)."""

    def __init__(self) -> None:
        self._rows: list[PreSubmitRecord] = []

    def append_presubmit(self, record: PreSubmitRecord) -> None:
        self._rows.append(record)

    def list_presubmit(self) -> tuple[PreSubmitRecord, ...]:
        return tuple(self._rows)


# ---------------------------------------------------------------------------
# SEC-005 scrubbing extension — POLY_* + L2 header secret values (never in any output)
# ---------------------------------------------------------------------------


def scrub_l2_output(text: str, *secrets: str) -> str:
    """Redact each L2 secret VALUE from ``text`` before it is logged/written (SEC-005 extension).

    Mirrors ``veridex.live_recorder.sources._scrub``: replaces raw secret values (the L2 ``owner``
    api-key UUID, the HMAC secret, the passphrase, and any signature) with ``[REDACTED]`` — so a
    ``POLY_API_KEY`` / ``POLY_SIGNATURE`` / ``POLY_PASSPHRASE`` value can never surface in output.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def scrub_headers_for_output(headers: Mapping[str, str], creds: L2ApiCredentials) -> dict[str, str]:
    """Return a copy of ``headers`` with the secret-bearing ``POLY_*`` values redacted (SEC-005)."""
    secrets = (*creds.secret_values(), headers.get("POLY_SIGNATURE", ""))
    return {k: scrub_l2_output(str(v), *secrets) for k, v in headers.items()}


# ---------------------------------------------------------------------------
# L2 HMAC canonicalization (§7b) — keyless: the L2 cred is an HMAC secret, not a signing key
# ---------------------------------------------------------------------------


def l2_hmac_signature(
    *, api_secret: str, timestamp: str, method: str, request_path: str, body: str
) -> str:
    """§7b HMAC-SHA256 over ``timestamp + method + requestPath + body`` (base64url secret + digest).

    ``api_secret`` is a base64url-encoded HMAC secret (NOT a private signing key). ``body`` is the EXACT
    canonical JSON wire string (already double-quoted, so the vendored single→double quote normalization
    is a no-op here); an empty body contributes nothing.
    """
    base64_secret = base64.urlsafe_b64decode(api_secret)
    message = f"{timestamp}{method}{request_path}"
    if body:
        message += body
    digest = hmac.new(base64_secret, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


def build_l2_headers(
    creds: L2ApiCredentials,
    *,
    address: str,
    timestamp: str,
    method: str,
    request_path: str,
    body: str,
) -> dict[str, str]:
    """Build the five §7b ``POLY_*`` L2 headers (``POLY_SIGNATURE`` is the HMAC over the exact body)."""
    signature = l2_hmac_signature(
        api_secret=creds.api_secret,
        timestamp=timestamp,
        method=method,
        request_path=request_path,
        body=body,
    )
    return {
        "POLY_ADDRESS": address,
        "POLY_SIGNATURE": signature,
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_API_KEY": creds.api_key,
        "POLY_PASSPHRASE": creds.api_passphrase,
    }


# ---------------------------------------------------------------------------
# Injected async HTTP boundary (recording-fake in tests; never a live venue)
# ---------------------------------------------------------------------------


@runtime_checkable
class RecordingHttpTransport(Protocol):
    """The injected async POST boundary — ONLY ever a RECORDING-FAKE in tests (no live venue)."""

    async def post(
        self, *, path: str, headers: dict[str, str], body: bytes
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class L2SubmitResult:
    """The result of one live submit: the persisted record + the EXACT wire bytes + headers + response."""

    presubmit_record: PreSubmitRecord
    wire_bytes: bytes
    headers: dict[str, str]
    response: dict[str, Any]


# ---------------------------------------------------------------------------
# The keyless L2 HMAC transport — OWNS the live commitment wiring
# ---------------------------------------------------------------------------


class KeylessL2Transport:
    """Keyless L2 HMAC transport that OWNS the compile→persist→sign→byte-verify→HMAC+send wiring.

    Holds NO local signing key. It signs orders ONLY through the injected Privy control plane and
    authenticates the HTTP call with an in-memory L2 HMAC secret (never a private key). The commitment
    control (persist-before-sign + submit-time byte-verify) is WIRED in :meth:`submit_live_order` here,
    not delegated to a helper elsewhere (Fable-MAJOR-1).
    """

    def __init__(
        self,
        *,
        control_plane: PrivyEvmWalletControlPlane,
        creds: L2ApiCredentials,
        http: RecordingHttpTransport,
        store: PreSubmitStore,
        now_s: Callable[[], int] | None = None,
    ) -> None:
        self._control_plane = control_plane
        self._creds = creds
        self._http = http
        self._store = store
        self._now_s = now_s if now_s is not None else (lambda: int(time.time()))

    async def submit_live_order(
        self,
        compiled: CompiledSigningPayload,
        *,
        binding: ExecutionWalletBinding,
        auth: PrivyAuthContext,
        request_path: str = "/order",
        method: str = "POST",
    ) -> L2SubmitResult:
        """Run the LIVE submit path (order load-bearing): compile→persist→sign→byte-verify→HMAC+send."""
        # (i) COMPILE done upstream; build the pre-sign integrity commitment over the entire body.
        commitment = OrderSigningCommitment.from_payload(compiled)

        # (ii) PERSIST the COMPOUND PreSubmitRecord (integrity digest AND the non-null venue_order_key,
        #      the official V2 order hash) to the append-only store BEFORE signing — NOT a bare digest.
        record = build_presubmit_record(compiled)
        self._store.append_presubmit(record)

        # (iii) SIGN via Privy (eth_signTypedData_v4). No local-key fallback exists in this graph.
        signature = self._control_plane.sign_typed_data(compiled, binding=binding, auth=auth)

        # Build the EXACT outgoing POST body: the compiled body + the one field added by signing.
        post_body = compiled.post_body()
        post_body["order"]["signature"] = signature.signature

        # (iv) BYTE-VERIFY the EXACT outgoing body against the pre-sign commitment — WIRED HERE.
        verify_post_body_against_commitment(post_body, commitment)

        # (v) Serialize ONCE to the exact wire bytes (no re-serialization after the byte-verify) and
        #     HMAC + SEND THOSE EXACT bytes.
        exact_post_bytes = serialize_payload(post_body).encode("utf-8")
        timestamp = str(self._now_s())
        headers = build_l2_headers(
            self._creds,
            address=binding.wallet_address,
            timestamp=timestamp,
            method=method,
            request_path=request_path,
            body=exact_post_bytes.decode("utf-8"),
        )
        response = await self._http.post(path=request_path, headers=headers, body=exact_post_bytes)
        return L2SubmitResult(
            presubmit_record=record,
            wire_bytes=exact_post_bytes,
            headers=headers,
            response=response,
        )


# ---------------------------------------------------------------------------
# Restart reconciliation: an ACK-lost fill JOINS back via the venue_order_key (Codex round-6)
# ---------------------------------------------------------------------------

#: A reader that knows ONLY the venue_order_key (fill history is keyed by the official V2 order id).
FillHistoryReader = Callable[[str], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class ReconciledFill:
    """One reconciled ACK-lost order: the join key + the tri-state verdict + the reconciled size."""

    venue_order_key: str
    reconciled_state: UncertainState
    reconciled_fill_size: float


def _coerce_fill_amount(value: Any) -> float | None:
    """Coerce a venue fill amount to a finite, NON-NEGATIVE float, or ``None`` if it is malformed.

    FAIL CLOSED to no-proof: a missing (``None``), unparseable (``"garbage"``), non-finite
    (``nan``/``inf``), or negative amount returns ``None`` — a component that is not a valid positive
    fill quantity is not proof of a fill and must never be summed into a partial (Gate#2 MAJOR-3).
    """
    if value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount) or amount < 0.0:
        return None
    return amount


def _match_fill_size(trades: Iterable[Mapping[str, Any]], venue_order_key: str) -> float | None:
    """Sum matched sizes for trades whose OFFICIAL id equals ``venue_order_key`` (None if no proof).

    Matches on the venue's official ids only (``taker_order_id`` when we are taker, or a
    ``maker_orders[].order_id`` when we are the resting maker) — never on Veridex's private digest.

    FAIL-CLOSED validation (single-source, so BOTH the restart-join and three-surface reconcilers
    benefit): every matched component must be finite and ``>= 0``; a malformed / non-finite / negative
    component degrades the WHOLE match to ``None`` (no proof, never a partial). Only a strictly
    POSITIVE aggregate returns a size — a zero (or fully-absent) matched total returns ``None`` too,
    since a zero venue amount is not positive fill proof (Gate#2 MAJOR-3).
    """
    matched = 0.0
    found = False
    for trade in trades:
        if str(trade.get("taker_order_id", "")) == venue_order_key:
            amount = _coerce_fill_amount(trade.get("size"))
            if amount is None:
                return None  # a matched-but-malformed/non-positive component → fail closed to no-proof
            matched += amount
            found = True
            continue
        for maker in trade.get("maker_orders", []) or []:
            if str(maker.get("order_id", "")) == venue_order_key:
                amount = _coerce_fill_amount(maker.get("matched_amount"))
                if amount is None:
                    return None
                matched += amount
                found = True
    if not found:
        return None
    return matched if matched > 0.0 else None


async def reconcile_ack_lost(
    store: PreSubmitStore, fill_reader: FillHistoryReader
) -> list[ReconciledFill]:
    """Reconcile ACK-lost pre-submit records against fill history keyed by the venue_order_key.

    On restart the ONLY durable link is the persisted ``venue_order_key`` (the official V2 order hash).
    For each persisted record we query the fill-history reader by that key: a matching fill resolves to
    ``RESOLVED`` (with size); no fill and no order resolves fail-closed to ``AMBIGUOUS`` (never fabricate
    a definitive verdict). A record that persisted only a private digest (no venue join key) cannot be
    queried and therefore cannot resolve — which is exactly why the compound record is required.
    """
    results: list[ReconciledFill] = []
    for record in store.list_presubmit():
        key = record.venue_order_key
        if not key:
            raise FailClosed(
                "pre-submit record has no venue_order_key — an ACK-lost fill cannot join fill history "
                "(the compound record must persist the official V2 order hash, Codex round-6)"
            )
        page = await fill_reader(key)
        trades = page.get("trades") or page.get("data") or []
        size = _match_fill_size(trades, key)
        if size is not None:
            results.append(ReconciledFill(key, "RESOLVED", size))
        else:
            results.append(ReconciledFill(key, "AMBIGUOUS", 0.0))
    return results


__all__ = [
    "POLY_L2_HEADER_NAMES",
    "FillHistoryReader",
    "InMemoryPreSubmitStore",
    "KeylessL2Transport",
    "L2SubmitResult",
    "PreSubmitStore",
    "ReconciledFill",
    "RecordingHttpTransport",
    "build_l2_headers",
    "l2_hmac_signature",
    "reconcile_ack_lost",
    "scrub_headers_for_output",
    "scrub_l2_output",
]
