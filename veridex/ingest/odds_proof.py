"""Merkle proof-status stamp for the odds feeding a sealed Veridex run ("verify-before-seal").

This is the INPUT-PROOF axis of verify-before-seal: for each odds message that fed a run, we ask
TxLINE (``GET /odds/validation?messageId=&ts=``) whether it can return a two-tier Merkle inclusion
proof (a ``subTreeProof`` committed into a ``mainTreeProof``) and record the per-message status as
REPORT-ONLY input-integrity metadata.

HONESTY BOUNDARY (hard requirement): the recorded/labeled claim is only
``"TxLINE returned valid Merkle inclusion proofs for N/M odds messages"``. It is NOT
"independently verified", "trustless", or "verified against the on-chain root" — confirming that
would require a FUTURE local ``subtree → maintree → root`` recompute against the txoracle Solana
root (Tier-2, out of scope here). This module confirms only that the API RETURNED a well-formed
inclusion proof, nothing stronger.

SEC-005: this is a report-only input-integrity field — it MUST NOT enter any ranking/scoring key.

Trust-path ``ingest/`` discipline (CON-010): the real :func:`~veridex.ingest.txline_client.validate_odds`
is lazy-imported only when no ``validate`` callable is injected, so this module carries no
import-time HTTP/network coupling and stays trivially testable offline.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # annotation only — never import backtest into ingest at runtime (no coupling/cycle)
    from veridex.backtest.report import BacktestReport

#: Per-message proof-status values (the ONLY values ``classify_proof`` / the summary emit).
PROVEN = "proven"  # non-empty subTreeProof AND mainTreeProof — a full two-tier inclusion proof
BOUNDARY = "boundary"  # empty subTreeProof, non-empty mainTreeProof — e.g. first pre-match record
ABSENT = "absent"  # missing/empty both, or a 404-sentinel — no proof exists for this record
ERROR = "error"  # transport/exception — could not be determined (fail-open, never voids the run)

#: Async signature of the injected validator; matches
#: :func:`veridex.ingest.txline_client.validate_odds` (``(message_id, ts, *, base_url, creds, client)``).
ValidateOddsFn = Callable[..., Awaitable[dict[str, Any]]]


def _nonempty_proof(proof: Any) -> bool:
    """True iff ``proof`` is a non-empty sequence of nodes (``{hash, isRightSibling}`` levels)."""
    return isinstance(proof, (list, tuple)) and len(proof) > 0


def classify_proof(resp: dict[str, Any]) -> str:
    """Classify one ``/odds/validation`` response by its Merkle inclusion-proof status.

    Returns one of :data:`PROVEN`, :data:`BOUNDARY`, :data:`ABSENT`, :data:`ERROR`:

      * :data:`PROVEN` — non-empty ``subTreeProof`` AND non-empty ``mainTreeProof``.
      * :data:`BOUNDARY` — empty ``subTreeProof`` but non-empty ``mainTreeProof`` (e.g. the first
        pre-match record, committed straight into the main tree with no sub-tree siblings).
      * :data:`ABSENT` — missing/empty BOTH proofs, ``None``, or a 404-sentinel
        (``{"_status": 404}`` / ``{"_not_found": True}``): no proof exists for this record.
      * :data:`ERROR` — an error-sentinel (``{"_error": ...}``) or an unexpected non-dict shape: the
        proof status could not be determined (transport/contract failure).

    Never raises — a malformed input degrades to :data:`ERROR` (honest "unknown"), never an
    exception that could VOID the run.
    """
    if resp is None:
        return ABSENT
    if not isinstance(resp, dict):
        return ERROR  # unexpected shape ⇒ contract/transport error, not a proof verdict
    if resp.get("_error"):
        return ERROR
    if resp.get("_status") == 404 or resp.get("_not_found"):
        return ABSENT
    sub_ok = _nonempty_proof(resp.get("subTreeProof"))
    main_ok = _nonempty_proof(resp.get("mainTreeProof"))
    if sub_ok and main_ok:
        return PROVEN
    if main_ok:  # empty sub, non-empty main ⇒ boundary (first pre-match record)
        return BOUNDARY
    return ABSENT


@dataclass(frozen=True)
class PerMessageProof:
    """The proof status for one odds message (``message_id`` → one of the status constants)."""

    message_id: str
    status: str


@dataclass(frozen=True)
class OddsProofSummary:
    """Aggregate proof-status stamp over the odds messages that fed a run (report-only)."""

    n_total: int
    n_proven: int
    n_boundary: int
    n_absent: int
    n_error: int
    per_message: list[PerMessageProof]

    def claim(self) -> str:
        """The HONEST, recorded claim string — see the module honesty boundary.

        "TxLINE returned valid Merkle inclusion proofs for N/M odds messages" — NOT
        "independently verified" / "trustless" / "verified against the on-chain root".
        """
        return (
            f"TxLINE returned valid Merkle inclusion proofs for {self.n_proven}/{self.n_total} "
            "odds messages"
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON/pydantic-friendly projection (what gets stored on the report field)."""
        return {
            "n_total": self.n_total,
            "n_proven": self.n_proven,
            "n_boundary": self.n_boundary,
            "n_absent": self.n_absent,
            "n_error": self.n_error,
            "claim": self.claim(),
            "per_message": [
                {"message_id": p.message_id, "status": p.status} for p in self.per_message
            ],
        }


async def summarize_odds_proofs(
    messages: Iterable[tuple[str, int]],
    *,
    validate: ValidateOddsFn | None = None,
    base_url: str | None = None,
    creds: tuple[str, str] | None = None,
    client: Any = None,
) -> OddsProofSummary:
    """Call ``validate_odds`` for each ``(message_id, ts)`` and aggregate the proof statuses.

    FAIL-OPEN (hard requirement): an exception on any single message maps THAT message to
    :data:`ERROR` and continues — this helper NEVER raises and NEVER voids the run. A run seals on
    its own evidence; the odds-proof stamp is report-only input integrity, so a proof lookup failing
    must not sink the run.

    Args:
        messages: The ``(message_id, ts)`` pairs for the odds messages that fed the run.
        validate: Injectable async validator (defaults to the real
            :func:`veridex.ingest.txline_client.validate_odds`, lazy-imported). Tests inject a mock
            so nothing hits the network.
        base_url, creds, client: Threaded through to ``validate`` (ignored by an injected mock that
            does not accept them — the default validator accepts them all as keyword-only).

    Returns:
        An :class:`OddsProofSummary` with per-status counts + a per-message breakdown.
    """
    validator: ValidateOddsFn
    if validate is None:
        from veridex.ingest.txline_client import validate_odds  # noqa: PLC0415

        validator = validate_odds
    else:
        validator = validate

    counts = {PROVEN: 0, BOUNDARY: 0, ABSENT: 0, ERROR: 0}
    per_message: list[PerMessageProof] = []
    for message_id, ts in messages:
        try:
            resp = await validator(message_id, ts, base_url=base_url, creds=creds, client=client)
            status = classify_proof(resp)
        except Exception:  # noqa: BLE0001 — fail-open: any lookup failure ⇒ ERROR, never re-raised
            status = ERROR
        counts[status] += 1
        per_message.append(PerMessageProof(message_id=message_id, status=status))

    return OddsProofSummary(
        n_total=len(per_message),
        n_proven=counts[PROVEN],
        n_boundary=counts[BOUNDARY],
        n_absent=counts[ABSENT],
        n_error=counts[ERROR],
        per_message=per_message,
    )


def attach_odds_proof_status(report: BacktestReport, summary: OddsProofSummary) -> BacktestReport:
    """Attach the odds-proof stamp to a report as REPORT-ONLY metadata (post-build ``model_copy``).

    Mirrors the estimated-edge post-build attach (see
    :func:`veridex.backtest.vvv_report.vvv_report_with_estimated_edge`): the pure venue/proof-free
    :func:`~veridex.backtest.report.build_backtest_report` NEVER sets this field, and it is NOT bound
    into ``config_hash`` / ``evidence_hash`` — so attaching it CANNOT perturb an existing sealed hash.
    It is never a ranked axis (SEC-005).

    FUTURE: this records only that TxLINE RETURNED valid inclusion proofs. Binding a locally
    recomputed ``subtree → maintree → root`` verdict (Tier-2, independent verification against the
    on-chain root) into the sealed evidence hash is deliberately OUT OF SCOPE here — report-only now.
    """
    return report.model_copy(update={"odds_proof_status": summary.as_dict()})
