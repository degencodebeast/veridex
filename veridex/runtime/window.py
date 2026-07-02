"""T7 — the live RunWindow contract + windowed-CLV honesty helpers (DEC-2D-1/2, REQ-2D-104, §4.1).

A PURE model + two helpers. This module imports NOTHING from the trust path beyond ``pydantic``
(no LLM SDK, no httpx, no MarketState) — it only describes a live run's coverage window and the two
honesty rules that govern windowed CLV:

  * ``clv_field_name`` (DEC-2D-1) — the mode label never lies. A ``pre_match`` window ends at
    kickoff and its close is the CON-040 *reconstructed* close, so its value is TRUE CLV
    (``clv_bps``). A ``fixed_duration``/``manual_stop`` window closes on the line AT window end,
    which is WINDOW CLV (``window_clv_bps``) — named distinctly EVERYWHERE so downstream can never
    mistake a window's in-play CLV for the true closing-line value.
  * ``is_pending_horizon`` (DEC-2D-2) — an action entered within ``min_clv_horizon_s`` of window
    close has too little runway to earn a meaningful closing move, so (exactly like WAIT) it is
    excluded from CLV means via the EXISTING ``"pending"`` sentinel rather than scored as a
    numeric 0.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, model_validator

#: The CLV value-field name a ``pre_match`` window uses — TRUE (reconstructed-close) CLV.
CLV_FIELD_TRUE = "clv_bps"
#: The CLV value-field name a ``fixed_duration``/``manual_stop`` window uses — WINDOW (in-play) CLV.
CLV_FIELD_WINDOW = "window_clv_bps"
#: The ``RunEvent.event_type`` a windowed run seals its coverage-window config under (T8c). Defined
#: HERE — the pure, trust-path-clean window module both the orchestrator (seal), the event-log
#: projection, and the Proof-Check re-derivation import, so the string can never drift between the
#: writer and the verifier. Emitted ONLY for windowed runs, so the legacy path stays byte-identical.
WINDOW_CONFIG_EVENT_TYPE = "window_config"


class RunWindow(BaseModel):
    """A live run's coverage window + its CLV end rule (§4.1).

    MUTABLE by design — unlike the frozen ``MarketState``/``AgentAction``. ``started_ts`` is stamped
    from the FIRST accepted tick at finalize time (it is ``None`` until a tick is fed), so the Proof
    Card's coverage window carries an EVIDENCE-derived start rather than a wall-clock guess.

    Attributes:
        window_id: Stable identifier for this window (used as the manifest ``fixture_or_window_id``).
        fixture_id: The TxLINE fixture this window covers.
        market_allowlist: ``market_key`` prefixes this window scores (e.g. ``["OU", "1X2"]``).
        end_rule: How the window closes —
            ``pre_match`` (ends at kickoff; close is the reconstructed CON-040 close → true CLV),
            ``fixed_duration`` (ends after ``duration_s``), or ``manual_stop`` (ends on demand).
        duration_s: Required IFF ``end_rule == "fixed_duration"`` (validated); must be ``None`` otherwise.
        min_clv_horizon_s: DEC-2D-2 horizon — an entry within this many seconds of close is excluded
            from CLV means (pending_horizon). Defaults to 60.
        started_ts: Evidence-derived window start; ``None`` until the first accepted tick stamps it.
    """

    window_id: str
    fixture_id: int
    market_allowlist: list[str]
    end_rule: Literal["pre_match", "fixed_duration", "manual_stop"]
    duration_s: int | None = None
    min_clv_horizon_s: int = 60
    started_ts: int | None = None

    @model_validator(mode="after")
    def _validate_duration(self) -> RunWindow:
        """``duration_s`` is present IFF ``end_rule`` is ``fixed_duration`` (§4.1).

        Both directions are enforced: a ``fixed_duration`` window without a duration is
        under-specified, and a duration on any other end rule is meaningless and could mislead a
        reader into thinking the window is time-bounded when it is not.
        """
        if self.end_rule == "fixed_duration" and self.duration_s is None:
            raise ValueError("duration_s is required when end_rule == 'fixed_duration'")
        if self.end_rule != "fixed_duration" and self.duration_s is not None:
            raise ValueError(
                f"duration_s must be None when end_rule == {self.end_rule!r} (only fixed_duration takes a duration)"
            )
        return self


def clv_field_name(end_rule: str) -> str:
    """The CLV value-field name for ``end_rule``: ``clv_bps`` iff ``pre_match``, else ``window_clv_bps``.

    DEC-2D-1 honesty doctrine: only a ``pre_match`` window reconstructs the real close, so only it
    yields TRUE CLV. ``fixed_duration``/``manual_stop`` close on the line at window end — that is
    WINDOW CLV, named distinctly so it can never be mistaken for the true closing-line value.
    """
    return CLV_FIELD_TRUE if end_rule == "pre_match" else CLV_FIELD_WINDOW


def is_pending_horizon(entry_ts: int, window_end_ts: int, min_clv_horizon_s: int) -> bool:
    """True when an action's entry is within ``min_clv_horizon_s`` of window close (DEC-2D-2).

    Boundary (documented): STRICTLY less-than is pending. An entry EXACTLY ``min_clv_horizon_s``
    before close (``window_end_ts - entry_ts == min_clv_horizon_s``) has just enough runway and is
    NOT pending; anything closer (``< min_clv_horizon_s``, including an entry AT close) is
    pending_horizon and is excluded from CLV means like WAIT.

    Args:
        entry_ts: The action's entry-tick ``ts``.
        window_end_ts: The window's closing-tick ``ts`` (the last accumulated snapshot).
        min_clv_horizon_s: The DEC-2D-2 horizon in seconds.

    Returns:
        ``True`` iff the entry is inside the horizon (excluded from CLV means).
    """
    return window_end_ts - entry_ts < min_clv_horizon_s
