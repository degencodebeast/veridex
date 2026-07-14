"""E3-T6 — owner-committed integrity commitment + submit-time POST-body byte-verify (REQ-018a).

MONEY-NETWORK BOUNDARY. This module is PURE: NO network, NO Privy, NO private-key type, NO real
signing. It only derives one-way digests over an intended CLOB-V2 ``SendOrder`` POST body and proves
that body was not mutated between the pre-sign commitment and the submit-time byte-verify.

Two digests, deliberately DISTINCT (v0.6.3 / Codex-M1, Codex-M2):

* ``integrity_commitment_hash`` — Veridex's PRIVATE one-way digest over the ENTIRE POST body
  **including ``owner``** (the L2 API-key UUID, a SECRET). It is
  ``sha256(serialize_payload(covered))`` over the schema-derived EXACT SET of the 16 covered fields.
  We persist ONLY this digest pre-sign (:meth:`OrderSigningCommitment.persist_digest_only`) — the raw
  ``owner`` is NEVER stored or logged (v0.6.3 / Codex-M1). At submit time the LIVE POST body (which
  transiently carries ``owner``) is re-hashed and compared; a mutated field OR an added/removed key
  fails closed.

* ``venue_order_key`` — the VENUE-recognized V2 order hash/id (the EIP-712 ``orderHash``,
  E3-T0 §3d), computed by :mod:`veridex.dust_execution.signing_compiler`. This is the reconciliation
  JOIN KEY (order/trade/fill responses are keyed by it), NOT the private integrity digest
  (v0.6.3 / Codex-M2). The two are structurally different digests (keccak EIP-712 vs sha256 canonical)
  and MUST NOT be conflated.

The covered field SET is SCHEMA-DERIVED from the E3-T0 pinned EXACT SETS in
:mod:`veridex.dust_execution.clobv2_gate` (``docs/maker/r4a-clobv2-wire-contract.md`` §1/§2), NOT a
hand-maintained list: an unknown/added key or a missing field fails closed. The ordered tuples in
:data:`SENDORDER_SCHEMA` are asserted at import to equal the gate's frozensets, so any drift between
the ordered signing view and the pinned schema is caught immediately (fail-closed doctrine).

The pre-sign persist and the submit-time byte-verify are WIRED into the live path by E3-T8; this module
provides the pure pieces (the commitment + the ``verify_post_body_against_commitment`` function).
"""

from __future__ import annotations

import hashlib
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from veridex.dust_execution.clobv2_gate import (
    _ORDER_WIRE_OPTIONAL,
    _ORDER_WIRE_REQUIRED,
    _SENDORDER_OPTIONAL,
    _SENDORDER_REQUIRED,
    _SIGNED_ORDER_FIELDS,
)
from veridex.dust_execution.contracts import PreSubmitRecord
from veridex.dust_execution.risk import FailClosed
from veridex.runtime.evidence import serialize_payload

if TYPE_CHECKING:  # pragma: no cover - typing only; NO runtime import (avoids a compiler cycle).
    from veridex.dust_execution.signing_compiler import CompiledSigningPayload


# ---------------------------------------------------------------------------
# Schema-derived EXACT SETS (from the E3-T0 pinned frozensets in clobv2_gate)
# ---------------------------------------------------------------------------

# The signing-order view of the signed struct (§1b). ORDER is load-bearing for EIP-712 encoding; the
# SET is asserted below to equal the gate's ``_SIGNED_ORDER_FIELDS`` so a drifted hand order fails.
_SIGNED_FIELDS_ORDERED: tuple[str, ...] = (
    "salt",
    "maker",
    "signer",
    "tokenId",
    "makerAmount",
    "takerAmount",
    "side",
    "signatureType",
    "timestamp",
    "metadata",
    "builder",
)

# The unsigned wrapper (§2): the wire body's unsigned order field(s) + the SendOrder top-level minus
# the nested ``order`` object. Derived from the gate frozensets, NOT hand-listed.
_UNSIGNED_WRAPPER_SET: frozenset[str] = frozenset(
    ((_ORDER_WIRE_REQUIRED | _ORDER_WIRE_OPTIONAL) - _SIGNED_ORDER_FIELDS - {"signature"})
    | (_SENDORDER_REQUIRED - {"order"})
    | _SENDORDER_OPTIONAL
)
# An ordered view for parametrized tests / covered_field_names (SET asserted below).
_UNSIGNED_WRAPPER_ORDERED: tuple[str, ...] = (
    "expiration",
    "orderType",
    "postOnly",
    "owner",
    "deferExec",
)

# Fail-closed self-checks: the ordered tuples MUST equal the schema-derived sets, else the pinned
# EXACT SET and this module have drifted apart (never silently accept a stale/hand list).
assert set(_SIGNED_FIELDS_ORDERED) == set(_SIGNED_ORDER_FIELDS), (
    "signed-field ordered view drifted from clobv2_gate._SIGNED_ORDER_FIELDS"
)
assert set(_UNSIGNED_WRAPPER_ORDERED) == _UNSIGNED_WRAPPER_SET, (
    "unsigned-wrapper ordered view drifted from the schema-derived unsigned set"
)

# EXACT-SET key allowlists for the POST body (fail-closed byte-verify).
# Top-level SendOrder keys the compiler always emits (required + optional emitted).
_TOPLEVEL_REQUIRED: frozenset[str] = _SENDORDER_REQUIRED | _SENDORDER_OPTIONAL
# ``order`` object: the full wire body set is ALLOWED (incl. ``signature``, the one field added by
# signing between commit and submit); everything except ``signature`` is REQUIRED at verify time.
_ORDER_ALLOWED: frozenset[str] = _ORDER_WIRE_REQUIRED | _ORDER_WIRE_OPTIONAL
_ORDER_REQUIRED: frozenset[str] = _ORDER_ALLOWED - {"signature"}

# Which covered fields live in the nested ``order`` object vs the SendOrder top level.
_COVERED_IN_ORDER: frozenset[str] = frozenset(_SIGNED_ORDER_FIELDS | {"expiration"})
_COVERED_TOP_LEVEL: frozenset[str] = frozenset(_UNSIGNED_WRAPPER_SET - {"expiration"})


@dataclass(frozen=True)
class SendOrderSchema:
    """The E3-T0 schema-derived signed/unsigned field partition of a CLOB-V2 ``SendOrder``.

    ``signed_fields`` are the §1b EIP-712 signed struct fields (in signing order); ``unsigned_wrapper``
    are the §2 wire-body/top-level fields that travel UNSIGNED (``expiration``, ``orderType``,
    ``postOnly``, ``owner``, ``deferExec``). Both are the pinned EXACT SETS — the byte-verify covers
    their UNION, so mutating any one (signed OR unsigned) fails closed.
    """

    signed_fields: tuple[str, ...]
    unsigned_wrapper: tuple[str, ...]

    @property
    def covered_fields(self) -> frozenset[str]:
        """The full covered EXACT SET (signed ∪ unsigned) the integrity commitment digests."""
        return frozenset(self.signed_fields) | frozenset(self.unsigned_wrapper)


#: The single pinned schema instance the compiler + commitment + tests share.
SENDORDER_SCHEMA = SendOrderSchema(
    signed_fields=_SIGNED_FIELDS_ORDERED,
    unsigned_wrapper=_UNSIGNED_WRAPPER_ORDERED,
)


# ---------------------------------------------------------------------------
# Covered-field extraction + integrity digest (pure, fail-closed EXACT SET)
# ---------------------------------------------------------------------------


def _keys(obj: Any, *, label: str) -> set[str]:
    """The exact key set of a mapping; fail closed on a non-mapping.

    Unlike the on-disk fixture loader (which tolerates ``_comment`` keys), a LIVE POST body must carry
    NO extra keys of any kind — a ``_``-prefixed key is just as much an unknown/added key here, so it
    is NOT ignored (an injected ``__unknown_added_key__`` must fail closed).
    """
    if not isinstance(obj, dict):
        raise FailClosed(f"{label}: expected an object, got {type(obj).__name__}")
    return set(obj)


def _enforce_exact_set(post_body: dict[str, Any]) -> None:
    """Fail closed unless the POST body carries EXACTLY the pinned key structure (Codex-M2).

    A digest comparison alone cannot catch an ADDED unknown key (it does not change the 16 covered
    fields), so the exact-set check is a REQUIRED second signal: an unknown/added key OR a missing
    required field at either level fails closed.
    """
    top_keys = _keys(post_body, label="post_body")
    for missing in sorted(_TOPLEVEL_REQUIRED - top_keys):
        raise FailClosed(f"post_body: missing required key {missing!r}")
    for unknown in sorted(top_keys - _TOPLEVEL_REQUIRED):
        raise FailClosed(f"post_body: unknown top-level key {unknown!r} (not in the V2 exact set)")

    order = post_body["order"]
    order_keys = _keys(order, label="post_body.order")
    for missing in sorted(_ORDER_REQUIRED - order_keys):
        raise FailClosed(f"post_body.order: missing required key {missing!r}")
    for unknown in sorted(order_keys - _ORDER_ALLOWED):
        raise FailClosed(
            f"post_body.order: unknown key {unknown!r} (not in the V2 order wire exact set)"
        )


def extract_covered_fields(post_body: dict[str, Any]) -> dict[str, Any]:
    """Flatten the 16 covered fields (signed ∪ unsigned, incl. ``owner``) from a POST body.

    Fail-closed EXACT SET: an unknown/added key or a missing covered field raises
    :class:`~veridex.dust_execution.risk.FailClosed`. ``owner`` IS covered (Codex-M1) so a mutated
    owner is detected; it is only ever hashed here, never returned to a persistence sink.
    """
    _enforce_exact_set(post_body)
    order = post_body["order"]
    covered: dict[str, Any] = {}
    for name in _COVERED_IN_ORDER:
        covered[name] = order[name]
    for name in _COVERED_TOP_LEVEL:
        covered[name] = post_body[name]
    return covered


def integrity_commitment_over(post_body: dict[str, Any]) -> str:
    """``sha256`` over the canonical serialization of the 16 covered fields (owner INCLUDED).

    This is the ``integrity_commitment_hash``: a one-way digest that COMMITS to ``owner`` but from
    which ``owner`` cannot be recovered, so persisting only the digest never leaks the secret.
    """
    covered = extract_covered_fields(post_body)
    return hashlib.sha256(serialize_payload(covered).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The frozen owner-committed commitment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderSigningCommitment:
    """A frozen pre-sign commitment: the owner-committed integrity digest + its covered field names.

    Holds ONLY the one-way ``integrity_commitment_hash`` (which committed to ``owner``) and the covered
    field NAMES — never the raw ``owner`` value. :meth:`persist_digest_only` writes only the digest so
    the secret is never stored (v0.6.3 / Codex-M1).
    """

    integrity_commitment_hash: str
    covered_field_names: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: CompiledSigningPayload) -> OrderSigningCommitment:
        """Commit over the ENTIRE POST body (including ``owner``) of a compiled payload.

        Recomputes the digest from ``payload.post_body()`` so the commitment is, by construction, the
        hash of the exact body about to be signed/submitted — and cross-checks it against the payload's
        own ``integrity_commitment_hash`` (fail closed on any drift).
        """
        body = payload.post_body()
        digest = integrity_commitment_over(body)
        if digest != payload.integrity_commitment_hash:
            raise FailClosed(
                "integrity_commitment_hash drift between compiled payload and recomputed POST body"
            )
        covered = tuple(SENDORDER_SCHEMA.signed_fields) + tuple(SENDORDER_SCHEMA.unsigned_wrapper)
        return cls(integrity_commitment_hash=digest, covered_field_names=covered)

    def covered_fields(self) -> set[str]:
        """The covered EXACT SET (signed ∪ unsigned) — 16 fields, incl. ``owner``."""
        return set(self.covered_field_names)

    def persist_digest_only(self, store: MutableMapping[str, Any]) -> None:
        """Persist ONLY the digest (+ covered names) pre-sign. NEVER writes the raw ``owner``.

        Structurally leak-proof: this commitment does not hold the raw ``owner``, so there is nothing
        to leak — only the one-way digest and the field names are written.
        """
        store["integrity_commitment_hash"] = self.integrity_commitment_hash
        store["covered_field_names"] = list(self.covered_field_names)


def verify_post_body_against_commitment(
    post_body: dict[str, Any], commitment: OrderSigningCommitment
) -> None:
    """Byte-verify a submit-time POST body against a pre-sign commitment; fail closed on ANY change.

    Two fail-closed signals (a digest-only check is NOT sufficient — it misses an added key that
    leaves the 16 covered fields untouched):

    1. EXACT-SET: the body must carry exactly the pinned key structure (unknown/added key → raise).
    2. DIGEST: re-hash the 16 covered fields (incl. the live ``owner``) and compare to the committed
       digest — a mutated field (signed OR unsigned) changes the digest → raise.
    """
    recomputed = integrity_commitment_over(post_body)  # runs the EXACT-SET check first
    if recomputed != commitment.integrity_commitment_hash:
        raise FailClosed(
            "POST body byte-verify failed: recomputed integrity digest does not match the pre-sign "
            "commitment (a covered field was mutated between commit and submit)"
        )


def build_presubmit_record(payload: CompiledSigningPayload) -> PreSubmitRecord:
    """Build the durable IDM-005 pre-submit record: integrity digest + venue join key (Codex-M2).

    ``venue_order_key`` is the venue-recognized V2 order hash (the reconciliation join key), DISTINCT
    from the private ``integrity_commitment_hash``. Fails closed if the two coincide (a sign that the
    join key was mistakenly set to the private digest).
    """
    if payload.venue_order_key == payload.integrity_commitment_hash:
        raise FailClosed(
            "venue_order_key must be the venue-recognized V2 order hash, NOT the private integrity "
            "digest (Codex-M2)"
        )
    return PreSubmitRecord(
        integrity_commitment_hash=payload.integrity_commitment_hash,
        venue_order_key=payload.venue_order_key,
        captured_id=None,
    )


__all__ = [
    "OrderSigningCommitment",
    "SENDORDER_SCHEMA",
    "SendOrderSchema",
    "build_presubmit_record",
    "extract_covered_fields",
    "integrity_commitment_over",
    "verify_post_body_against_commitment",
]
