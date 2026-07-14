"""E7-T5: DIAGNOSTIC post-trade markout + mandatory honesty labels for the R4-A dust lane.

Everything in this module is **diagnostic-only**. Nothing here is a scored or ranked field, and
nothing here mutates a sealed lifecycle event:

* :func:`compute_markout` derives a **new** :class:`~veridex.dust_execution.contracts.PostTradeMarkoutEvent`
  keyed by ``decision_id`` from a SEALED own-fill event WITHOUT mutating that sealed event — it
  reads the fill, it never writes back. The output field names (``reference_price`` / ``markout_bps``)
  are already on the SEC-006 rank denylist (:data:`veridex.rank_guards.R4A_EXECUTION_DENYLIST_FIELDS`,
  E1-T5), so a markout can NEVER reach ``veridex.scoring`` / ``veridex.leaderboard`` /
  ``veridex.maker.leaderboard`` (AC-014).
* :data:`REQUIRED_DUST_RUN_LABELS` / :func:`assert_mandatory_dust_run_labels` enforce the mandatory
  honesty labels every dust run must carry: ``DUST_LIVE`` / the evidence class / ``UNCALIBRATED`` /
  ``NOT_PROVEN_EDGE`` (AC-025). The pinned Literals on
  :class:`~veridex.dust_execution.contracts.DustRunLabelEvent` make a softened label unconstructable;
  the checker rejects any label-like object that downgrades a mandatory value.
* :func:`label_for_scoped_negative` / :func:`reject_scoped_negative_relabel` make a scoped-negative
  finding (a dust run that proved SAFETY, not alpha) UNPROMOTABLE: its evidence class is
  STRUCTURALLY pinned to ``EXPERIMENTAL_DUST`` (there is no parameter to set it), and an explicit
  promotion request (``EVIDENCE_GATED`` / ``PROMOTED``) from ANY request field / label input / LLM
  output / metadata is REFUSED fail-closed (SEC-001, CON-003). R4-A never claims proven alpha; only
  Gate B (out of R4-A scope) controls promotion.

Like ``contracts.py``, this module imports ONLY from ``veridex.dust_execution.contracts`` and the
standard library — it never imports a ranked/maker/scoring lane, so it cannot cross the SEC-003
isolation boundary.
"""

from __future__ import annotations

from typing import Final, Literal

from veridex.dust_execution.contracts import (
    DustRunLabelEvent,
    EvidenceClass,
    OwnFillEvent,
    PostTradeMarkoutEvent,
)

__all__ = [
    "REQUIRED_DUST_RUN_LABELS",
    "SCOPED_NEGATIVE_EVIDENCE_CLASS",
    "MarkoutError",
    "ScopedNegativeRelabelError",
    "assert_mandatory_dust_run_labels",
    "compute_markout",
    "label_for_scoped_negative",
    "reject_scoped_negative_relabel",
]


# --- Diagnostic markout -------------------------------------------------------------------


class MarkoutError(ValueError):
    """Raised when a markout operand is not usable (out-of-[0,1] price, or an unknown side)."""


#: BUY is favorable when the reference RISES above the fill; SELL when it FALLS below it.
_SIDE_SIGN: Final[dict[str, int]] = {"BUY": 1, "SELL": -1}

#: Basis-points scale for a native-probability move (a full 0→1 move is 10,000 bps).
_BPS_SCALE: Final[float] = 1e4


def _reject_price_out_of_unit_interval(value: float, name: str) -> float:
    """Return ``value`` if it is a native probability in ``[0, 1]``, else raise ``MarkoutError``.

    Mirrors the native-``[0,1]`` price guard used across the lane (CON-004): a decimal-odds-style
    operand (e.g. ``1.4``) can never reach the markout arithmetic and silently mis-scale it.
    """
    if 0.0 <= value <= 1.0:
        return value
    raise MarkoutError(f"{name} must be a native probability in [0, 1], got {value!r}")


def _markout_sign(side: str) -> int:
    """Signed direction for the markout: ``+1`` for a BUY, ``-1`` for a SELL (case-insensitive)."""
    try:
        return _SIDE_SIGN[side.strip().upper()]
    except KeyError:
        raise MarkoutError(f"unknown side {side!r}; expected BUY or SELL") from None


def compute_markout(
    fill_event: OwnFillEvent,
    *,
    reference_price: float,
    horizon_ms: int,
    sequence_no: int,
    recv_ts: int,
) -> PostTradeMarkoutEvent:
    """Derive a DIAGNOSTIC post-trade markout keyed by ``fill_event.decision_id``.

    The markout is the signed native-probability move (in basis points) between the realized
    ``fill_price`` and a later ``reference_price``, signed by side (a BUY is favorable when the
    reference rises; a SELL when it falls). Both prices are bounds-checked BEFORE the arithmetic.

    This reads the SEALED ``fill_event`` and returns a **new** record; it NEVER mutates the fill (the
    fill is a frozen contract regardless, but nothing here even attempts a write-back). The returned
    :class:`PostTradeMarkoutEvent` is diagnostic-only: its ``reference_price`` / ``markout_bps`` are
    on the SEC-006 rank denylist, so it can never enter a rank input (AC-014).

    Args:
        fill_event: The sealed realized own-fill the markout is derived from (source of the join
            key ``decision_id``, the ``side``, and the realized ``fill_price``).
        reference_price: The later fair-value reference, a native probability in ``[0, 1]``.
        horizon_ms: The markout horizon in integer milliseconds.
        sequence_no: The numbered-stream position for the emitted diagnostic event.
        recv_ts: The recorder-clock receive time in integer milliseconds.

    Returns:
        A new :class:`PostTradeMarkoutEvent` keyed by ``fill_event.decision_id``.

    Raises:
        MarkoutError: If ``reference_price`` is outside ``[0, 1]`` or ``fill_event.side`` is unknown.
    """
    reference_price = _reject_price_out_of_unit_interval(reference_price, "reference_price")
    fill_price = _reject_price_out_of_unit_interval(fill_event.fill_price, "fill_price")
    sign = _markout_sign(fill_event.side)
    markout_bps = sign * (reference_price - fill_price) * _BPS_SCALE
    return PostTradeMarkoutEvent(
        sequence_no=sequence_no,
        event_type="PostTradeMarkoutEvent",
        source_ts=None,
        recv_ts=recv_ts,
        decision_id=fill_event.decision_id,
        horizon_ms=horizon_ms,
        reference_price=reference_price,
        markout_bps=markout_bps,
    )


# --- Mandatory honesty labels -------------------------------------------------------------


#: The mandatory honesty-label VALUES every dust run must carry (AC-025). Keyed by the
#: ``DustRunLabelEvent`` field name → its required pinned value. The evidence class is deliberately
#: NOT here: it is a closed set (:data:`EvidenceClass`), guarded separately for scoped-negatives.
REQUIRED_DUST_RUN_LABELS: Final[dict[str, str]] = {
    "run_label": "DUST_LIVE",
    "calibration_label": "UNCALIBRATED",
    "edge_label": "NOT_PROVEN_EDGE",
}


def assert_mandatory_dust_run_labels(label: object) -> None:
    """Fail closed unless ``label`` carries every mandatory honesty-label value (AC-025).

    A :class:`DustRunLabelEvent` already pins these via Literals (a softened label is unconstructable),
    so this is defense-in-depth: it rejects ANY label-like object (e.g. a downgraded mirror) that
    weakens a mandatory value before it can narrate a run as calibrated / proven / non-live.

    Raises:
        AssertionError: If a mandatory label field is missing or downgraded.
    """
    for field, expected in REQUIRED_DUST_RUN_LABELS.items():
        actual = getattr(label, field, None)
        if actual != expected:
            raise AssertionError(
                f"mandatory dust-run label {field!r} must be {expected!r}, got {actual!r}"
            )


# --- Scoped-negative relabel-proof (SEC-001, CON-003) -------------------------------------


#: A scoped-negative finding (safety proven, alpha NOT proven) is intrinsically this evidence class.
#: There is no code path in R4-A that moves it higher — promotion is a Gate B concern, out of scope.
SCOPED_NEGATIVE_EVIDENCE_CLASS: Final[Literal["EXPERIMENTAL_DUST"]] = "EXPERIMENTAL_DUST"

#: The promoted evidence classes a scoped-negative finding may NEVER be relabelled to within R4-A.
_PROMOTED_EVIDENCE_CLASSES: Final[frozenset[str]] = frozenset({"EVIDENCE_GATED", "PROMOTED"})


class ScopedNegativeRelabelError(RuntimeError):
    """Raised when any input tries to relabel a scoped-negative finding to a promoted evidence class."""


def reject_scoped_negative_relabel(requested: object) -> None:
    """Fail closed if ``requested`` asks to relabel a scoped-negative finding as promoted.

    ``requested`` is UNTRUSTED — it may be a request field, a label input, an LLM output, or a
    metadata blob. It is coerced to text and scanned for a promoted evidence-class token, so a
    sentence ("...PROMOTE this to EVIDENCE_GATED") and a metadata dict (``{"evidence_class":
    "PROMOTED"}``) are both refused. ``None`` and any non-promotion value are a NO-OP.

    Raises:
        ScopedNegativeRelabelError: If a promoted evidence-class token appears in ``requested``.
    """
    if requested is None:
        return
    text = str(requested).upper()
    offending = sorted(token for token in _PROMOTED_EVIDENCE_CLASSES if token in text)
    if offending:
        raise ScopedNegativeRelabelError(
            f"a scoped-negative dust finding cannot be relabelled {offending} — R4-A proves safety, "
            "not alpha; only Gate B (out of R4-A scope) controls promotion"
        )


def label_for_scoped_negative(
    *,
    sequence_no: int,
    recv_ts: int,
    requested_relabel: object = None,
) -> DustRunLabelEvent:
    """Emit the mandatory honest label for a scoped-negative dust run.

    The evidence class is STRUCTURALLY pinned to :data:`SCOPED_NEGATIVE_EVIDENCE_CLASS`
    (``EXPERIMENTAL_DUST``) — there is no parameter that can set it to anything else, so no request /
    label / LLM output / metadata can relabel the finding. ``requested_relabel`` exists ONLY so an
    explicit promotion attempt is caught and REFUSED fail-closed (defense-in-depth) rather than
    silently ignored.

    Args:
        sequence_no: The numbered-stream position for the label event.
        recv_ts: The recorder-clock receive time in integer milliseconds.
        requested_relabel: An UNTRUSTED relabel request; a promotion attempt raises.

    Returns:
        A :class:`DustRunLabelEvent` with the mandatory honest labels and ``EXPERIMENTAL_DUST``.

    Raises:
        ScopedNegativeRelabelError: If ``requested_relabel`` asks for a promoted evidence class.
    """
    reject_scoped_negative_relabel(requested_relabel)
    evidence_class: EvidenceClass = SCOPED_NEGATIVE_EVIDENCE_CLASS
    return DustRunLabelEvent(
        sequence_no=sequence_no,
        event_type="DustRunLabelEvent",
        source_ts=None,
        recv_ts=recv_ts,
        run_label="DUST_LIVE",
        evidence_class=evidence_class,
        calibration_label="UNCALIBRATED",
        edge_label="NOT_PROVEN_EDGE",
    )
