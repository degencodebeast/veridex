"""Pure-tier neutral strategy contracts (MM-R4-B).

Frozen pydantic v2 contracts the entire deterministic strategy tier is built on: the per-tick
:class:`StrategyObservation` (universal match-state leg + OPTIONAL guard FV leg), the carry-forward
:class:`StrategyState`, the provenance-bound :class:`StrategyDecision`, the size-free
:class:`NeutralIntent`, the closed :data:`DecisionKind` / :data:`ReasonCode` vocabularies, the
market-status alphabet + read-only :class:`MarketStatusAuthority` protocol, and the typed replay
:class:`MarketStatusEvent` row. Covers REQ-020/021/025/060/063 and the construction invariants.

Import whitelist (load-bearing — this module is the pure-tier BASE every sibling imports FROM):
stdlib + pydantic + ``veridex.runtime.evidence`` ONLY. ``serialize_payload`` is the single
canonical byte serializer and the SOLE ``veridex.*`` runtime dependency, so ``observation_hash`` /
``state_hash`` are byte-identical across processes. No sibling ``mm_strategy`` module and nothing
from ``dust_execution`` / ``live_recorder`` / ``venues`` / ``maker`` / ``scoring`` / ``research`` /
``ingest`` / ``policy`` — nor any LLM / network / signer surface (enforced by the E1-T1 audit).

Design invariants worth stating once:

* **Guard-off carries no FV element anywhere (Codex-R5 MAJOR-1).** The whole FV leg is a single
  optional nested :class:`GuardFairValue` (``guard_fv``). When the guard is off the assembler emits
  ``guard_fv=None`` regardless of feed health, so no ``fv`` / ``fv_source_ts`` / ``fv_source_epoch``
  value — or field — exists at the observation top level, and the baseline hash chain is
  byte-identical across healthy / stale / absent / reconnecting FV.
* **Sentinels are typed, not magic integers (Codex-R3 MAJOR-1).** ``market_status_recv_ts`` and
  ``market_status_epoch`` are ``int | None``, ``None`` *iff* ``market_status == "UNKNOWN"``.
* **Fail-closed clocks (REQ-022).** Any ``recv_ts``-bearing field ahead of ``as_of_ts`` is a
  construction error — an unconstructible input never becomes an observation.
* **No size (REQ-057).** :class:`NeutralIntent` carries no size field; ``resolve_dust_size`` remains
  the sole wire-size authority and decision identity is over side + price only.
"""

from __future__ import annotations

import hashlib
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from veridex.runtime.evidence import serialize_payload

# --- Closed vocabularies ------------------------------------------------------------------

# Venue/market status alphabet (REQ-020(e)). Provenance-bound; never inferred from book shape.
MarketStatus = Literal["ACTIVE", "HALTED", "CLOSED", "UNKNOWN"]

# Raw top-of-book health, reusing the existing live-recorder ``top_of_book`` vocabulary verbatim.
BookStatus = Literal["ok", "gap", "excluded"]

# Neutral (venue-agnostic) intent verbs the adapter maps onto the R4-A execution surface.
NeutralIntentKind = Literal["place_quote", "replace_quote", "cancel_all_orders", "abstain"]

# The CLOSED v0 decision taxonomy (REQ-060). ``WIDEN`` and ``TAKE`` are CUT from v0 (Fable F3 /
# REQ-061/062) — adding either is a spec revision, never task-plan discretion.
DecisionKind = Literal[
    "QUOTE_TWO_SIDED",
    "QUOTE_ONE_SIDED",
    "ONE_SIDED_REDUCE",
    "NO_QUOTE",
    "HOLD",
]

# The CLOSED reason vocabulary, §4.4 VERBATIM and in declared (deterministic) order (REQ-063).
# Reason codes enter decisions, hashes, telemetry, denylists, and replay, so ANY add/remove/rename
# is behavior-bearing and requires a spec/config revision (Codex-plan-review MAJOR-5 attack 3).
ReasonCode = Literal[
    "stale_observation",
    "clock_regression",
    "epoch_regression",
    "token_mapping_missing",
    "book_gap",
    "book_excluded",
    "book_stale",
    "book_thin",
    "level_count_low",
    "leg_skew",
    "boundary_zone",
    "leg_out_of_zone",
    "two_sided_zone_exit",
    "tick_regime_changed",
    "phase_transition",
    "market_status_unknown",
    "market_halted",
    "market_closed",
    "stream_degraded",
    "projection_stale",
    "event_cooldown",
    "event_ref_warmup",
    "txline_missing",
    "txline_stale",
    "txline_suspended",
    "basis_warmup",
    "prematch_basis_exceeds_spread",
    "residual_pull_ask",
    "residual_pull_bid",
    "residual_extreme",
    "inventory_reduce",
    "reduce_conflict",
    "hold_unchanged",
    "plan_frozen_pending_reconcile",
    "cancel_exposure_first",
]

# The closed, lowercase FV proof-status set (mirrors ``live_recorder`` / ``ingest.odds_proof``
# verbatim; re-declared here because the pure tier may not import that module). Never fabricated.
ProofStatus = Literal[
    "proven",
    "boundary",
    "absent",
    "error",
    "unavailable_no_message_id",
]


def _reject_price_out_of_unit_interval(value: float) -> float:
    """Native ``[0,1]`` price guard: rejects decimal-odds-style values (e.g. ``1.4``)."""
    if not (0.0 <= value <= 1.0):
        raise ValueError(f"price must be a native probability in [0, 1], got {value!r}")
    return value


class _FrozenModel(BaseModel):
    """Shared base: immutable, no extra fields tolerated, canonical evidence hash.

    ``_canonical_hash()`` hashes the canonical serialization of ``model_dump()`` via the shared
    ``veridex.runtime.evidence.serialize_payload`` (sorted keys, compact separators) so the same
    content yields the same hash in every process. Named public hashes (``observation_hash`` /
    ``state_hash``) delegate here so there is exactly ONE byte authority (REQ-040).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    def _canonical_hash(self) -> str:
        canonical = serialize_payload(self.model_dump())
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Guard FV leg (OPTIONAL as a whole) ---------------------------------------------------


class GuardFairValue(_FrozenModel):
    """The optional TxLINE guard leg (REQ-020(d2)). Present ONLY when the guard is config-enabled;
    a guard-off observation sets ``guard_fv=None`` so no FV element exists in the baseline arm.

    ``fv`` is native ``[0,1]``; ``fv_source_ts`` is the source clock (integer seconds), ``fv_recv_ts``
    the recorder clock (integer milliseconds); ``fv_source_epoch`` is the guard-scoped source
    generation (moved HERE — Codex-R5 MAJOR-1). A proof status may never be fabricated with no
    ``message_id`` to anchor it: ``message_id is None`` ⇒ ``proof_status == "unavailable_no_message_id"``.
    """

    fv: float
    fv_source_ts: int
    fv_recv_ts: int
    fv_source_epoch: int
    message_id: str | None
    proof_status: ProofStatus

    @field_validator("fv")
    @classmethod
    def _fv_in_unit_interval(cls, value: float) -> float:
        return _reject_price_out_of_unit_interval(value)

    @model_validator(mode="after")
    def _proof_reference_is_honest(self) -> GuardFairValue:
        if self.message_id is None and self.proof_status != "unavailable_no_message_id":
            raise ValueError(
                "proof_status must be 'unavailable_no_message_id' when message_id is None; "
                f"got {self.proof_status!r}"
            )
        return self


# --- Inventory / open-order projection ----------------------------------------------------


class RestingOrderView(_FrozenModel):
    """One resting open order supplied by the orchestration-layer inventory projection (REQ-020(g))."""

    client_order_id: str
    side: str
    price: float
    size: float

    @field_validator("price")
    @classmethod
    def _price_in_unit_interval(cls, value: float) -> float:
        return _reject_price_out_of_unit_interval(value)


class InventoryProjection(_FrozenModel):
    """Typed net-position + resting-order snapshot the decision reasons over (REQ-020(g))."""

    net_position: float
    resting: tuple[RestingOrderView, ...]
    projection_as_of_ts: int
    fresh: bool


# --- Observation --------------------------------------------------------------------------


class StrategyObservation(_FrozenModel):
    """The per-tick market view the pure strategy decides from (REQ-020).

    Carries RAW venue facts only — NO producer-computed decision feature (the event smoother and
    the rolling spread/depth references live in :class:`StrategyState`, updated by the pure core).
    """

    # (a) identity
    fixture_id: int
    market_ref: str
    side: str
    token_id: str
    venue_market_ref: str
    tick_size: float
    # (b) ordering & source epoch (the universal book generation; the FV epoch lives in guard_fv)
    observation_sequence: int
    book_source_epoch: int
    # (c) venue top-of-book — RAW facts only (bid/ask/sizes are None on a degraded book)
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    book_status: BookStatus
    status_reason: str | None
    book_recv_ts: int
    level_count_in_band: int
    tick_regime_changed: bool
    # (d) universal match-state leg — consumed IDENTICALLY by both ablation arms
    phase: int
    suspended: bool
    match_state_recv_ts: int
    # (d2) guard FV leg — OPTIONAL as a whole; None in a guard-off (baseline) observation
    guard_fv: GuardFairValue | None = None
    # (e) market status — typed sentinels: recv_ts/epoch are None IFF status == UNKNOWN
    market_status: MarketStatus
    market_status_recv_ts: int | None
    market_status_epoch: int | None
    # (f) stream health
    order_stream_ok: bool
    projection_fresh: bool
    # (g) inventory / open-order projection
    inventory: InventoryProjection
    # (h) the single clock the decision is evaluated at
    as_of_ts: int

    @field_validator("phase")
    @classmethod
    def _phase_is_binary(cls, value: int) -> int:
        # Match-state phase is binary (0|1) per REQ-020(d).
        if value not in (0, 1):
            raise ValueError(f"phase must be 0 or 1, got {value!r}")
        return value

    @model_validator(mode="after")
    def _status_sentinel_iff_unknown(self) -> StrategyObservation:
        # Codex-R3 MAJOR-1: the optional status fields are None IFF the status is UNKNOWN.
        is_unknown = self.market_status == "UNKNOWN"
        has_none = self.market_status_recv_ts is None or self.market_status_epoch is None
        all_none = self.market_status_recv_ts is None and self.market_status_epoch is None
        if is_unknown and not all_none:
            raise ValueError(
                "market_status == UNKNOWN requires market_status_recv_ts and "
                "market_status_epoch to be None"
            )
        if not is_unknown and has_none:
            raise ValueError(
                f"market_status == {self.market_status} requires non-None "
                "market_status_recv_ts and market_status_epoch"
            )
        return self

    @model_validator(mode="after")
    def _no_future_dated_timestamp(self) -> StrategyObservation:
        # REQ-022 fail-closed: every recv_ts-bearing field must be at or before as_of_ts.
        offenders: list[str] = []
        if self.book_recv_ts > self.as_of_ts:
            offenders.append("book_recv_ts")
        if self.match_state_recv_ts > self.as_of_ts:
            offenders.append("match_state_recv_ts")
        if (
            self.market_status_recv_ts is not None
            and self.market_status_recv_ts > self.as_of_ts
        ):
            offenders.append("market_status_recv_ts")
        if self.guard_fv is not None and self.guard_fv.fv_recv_ts > self.as_of_ts:
            offenders.append("guard_fv.fv_recv_ts")
        if offenders:
            raise ValueError(
                f"future-dated timestamp(s) ahead of as_of_ts={self.as_of_ts}: "
                f"{', '.join(offenders)}"
            )
        return self

    def observation_hash(self) -> str:
        """``sha256`` hexdigest over the canonical serialization (REQ-020 / REQ-040)."""
        return self._canonical_hash()


# --- State --------------------------------------------------------------------------------


class OutcomeAccumulator(_FrozenModel):
    """Per-``(market_ref, side)`` accumulator container (REQ-031).

    E1-T2 pins the outcome identity; the estimator/smoother/rolling-reference payload fields are
    added by E2-T2 (event smoother + rolling references) as append-only accumulator state — this
    is the frozen shell they extend.
    """

    market_ref: str
    side: str


class GuardStateWatermark(_FrozenModel):
    """The guard-SCOPED state watermark (REQ-031). Present ONLY when the guard is enabled — a
    guard-off state sets ``StrategyState.guard_watermark=None`` so no FV element exists anywhere in
    the baseline state, mirroring the observation's optional guard leg (Codex-R5 MAJOR-1)."""

    fv_source_epoch: int


class StrategyState(_FrozenModel):
    """The frozen, canonically-hashable carry-forward state threaded through ``decide()`` (REQ-031).

    Holds the watermark (REQ-020(b)/022/026/033/034), the per-outcome accumulators, an audit echo
    of the inventory projection used, and quote lineage. All watermark fields default to ``None`` so
    a fresh state accepts its first observation; the reducer tasks (E2-T3/E2-T4) own the acceptance
    logic and extend the accumulator payload.
    """

    # Ordering / epoch / clock watermark (None on a fresh state).
    last_observation_sequence: int | None = None
    last_book_source_epoch: int | None = None
    last_as_of_ts: int | None = None
    last_market_status_epoch: int | None = None
    last_market_status_recv_ts: int | None = None
    # Guard-scoped watermark — None in a guard-off state (no FV element anywhere).
    guard_watermark: GuardStateWatermark | None = None
    # Per-outcome accumulators + audit echo + quote lineage.
    outcomes: tuple[OutcomeAccumulator, ...] = ()
    inventory_echo: InventoryProjection | None = None
    quote_lineage: tuple[RestingOrderView, ...] = ()
    # Event smoother + rolling reference accumulators (E2-T2; REQ-036/080). STATE-carried, updated
    # by the pure core from RAW venue facts ONLY — never producer-supplied (RED-44). All default to
    # the empty/None post-reset seed so a fresh state (and the purity fixture) constructs unchanged;
    # the reducer (E2-T3/E2-T4) seeds ``smoother_mid`` from a row-R reset frame's own ``ok``-book
    # mid and appends RAW spread/depth samples, warmup-bound by ``config.ref_min_samples``.
    smoother_mid: float | None = None
    smoother_mid_ts: int | None = None
    spread_ref_samples: tuple[float, ...] = ()
    depth_ref_samples: tuple[float, ...] = ()
    # Bounded basis sample window (REQ-031/072): the ordered (oldest→newest) accepted
    # ``(as_of_ts_ms, raw_gap)`` samples the config-selected estimator reduces (``basis.basis``).
    # STATE-carried like the venue accumulators; the watermark layer (E2-T3) RESETS it (a full
    # REQ-033 reset clears it alongside the venue accumulators; an ``fv_source_epoch`` increment
    # resets it ALONE — the FV-independent venue accumulators are untouched, Codex-R5 MAJOR-1), and
    # the E2-T4 reducer APPENDS accepted samples. Empty is the post-reset / fresh-state seed.
    basis_samples: tuple[tuple[int, float], ...] = ()
    # Bounded EWMA sufficient accumulator (REQ-031/070/072; Codex Gate#1-R2 MAJOR-1): the running
    # time-decayed basis estimate + its last-accepted ``as_of_ts``, folded ONE admitted sample at a
    # time by the core so ``basis_estimator == "halflife_ewma"`` stays the spec's ONLINE time-decayed
    # estimator instead of degrading into a finite last-``basis_window`` window when the raw prefix is
    # dropped. Used ONLY by the EWMA arm (``rolling_median`` reads ``basis_samples``); both are cleared
    # by every basis reset (full REQ-033 reset, ``fv_source_epoch`` increment, row-R reset) exactly
    # where ``basis_samples`` is, and both default to ``None`` so a fresh state (and the purity
    # fixture) constructs unchanged.
    basis_ewma_value: float | None = None
    basis_ewma_ts: int | None = None
    # Event-cooldown deadline (REQ-081): the ``as_of_ts`` (observation clock, NEVER wall clock)
    # BEFORE which no (re)placement may occur after a reset/event trigger. The E2-T4 reducer ANCHORS
    # it at ``as_of_ts + book_state_dwell_before_quote_ms`` on a row-R reset / row-E event and reads
    # it back to classify a row-C frame (``as_of_ts < event_cooldown_until_ts``); an admitting row
    # (F/W/H) whose frame has passed the deadline clears it to ``None`` (warmup NEVER anchors one —
    # the liveness guarantee E2-T5 proves). ``None`` is the no-cooldown / fresh-state seed.
    event_cooldown_until_ts: int | None = None

    @model_validator(mode="after")
    def _ewma_accumulator_pair(self) -> StrategyState:
        # Codex Gate#1-R3 MAJOR-1: basis_ewma_value/basis_ewma_ts are a semantic pair — both
        # absent (fresh/reset state) or both present (a folded sample). A partial pair would
        # otherwise survive construction and silently lose EWMA history on the next admitted
        # sample instead of failing closed (REQ-031/035).
        if (self.basis_ewma_value is None) != (self.basis_ewma_ts is None):
            raise ValueError(
                "basis_ewma_value and basis_ewma_ts must both be None or both be set, got "
                f"basis_ewma_value={self.basis_ewma_value!r}, basis_ewma_ts={self.basis_ewma_ts!r}"
            )
        return self

    def state_hash(self) -> str:
        """``sha256`` hexdigest over the canonical serialization (REQ-031 / REQ-040)."""
        return self._canonical_hash()


# --- Neutral intent + decision ------------------------------------------------------------


class NeutralIntent(_FrozenModel):
    """One venue-agnostic intent the adapter maps onto R4-A (REQ-055/056/057).

    NO size field of any kind: R4-B v0 proposes no size (REQ-057) — ``resolve_dust_size`` is the
    sole wire-size authority and decision identity is over side + price only. ``post_only`` is True
    for every ``make_quote`` intent (REQ-056); ``price`` is None for ``cancel_all_orders`` / ``abstain``.
    """

    kind: NeutralIntentKind
    leg_role: Literal["bid", "ask", "reduce"] | None
    price: float | None = None
    post_only: bool = True
    client_order_id: str | None = None
    replaces_client_order_id: str | None = None

    @field_validator("price")
    @classmethod
    def _price_in_unit_interval(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _reject_price_out_of_unit_interval(value)


class StrategyDecision(_FrozenModel):
    """The pure strategy's provenance-bound output — the ONE decision name (Fable F8; REQ-025).

    Binds the four causal hashes (``observation_hash`` / ``config_hash`` / ``prior_state_hash`` /
    ``next_state_hash``), the ordered closed ``reason_codes``, the neutral ``intent_plan``, and the
    FV ``message_id`` / ``proof_status`` pass-through (populated only when the guard leg is present;
    proof availability has ZERO effect on the numeric decision). Every field defaults so the E1-T1
    ``core`` stub can construct a fixed HOLD; the real ``decide()`` (E2) populates them.
    """

    decision_id: str = ""
    kind: DecisionKind = "HOLD"
    reason_codes: tuple[ReasonCode, ...] = ()
    intent_plan: tuple[NeutralIntent, ...] = ()
    observation_hash: str = ""
    config_hash: str = ""
    prior_state_hash: str = ""
    next_state_hash: str = ""
    fv_message_id: str | None = None
    fv_proof_status: ProofStatus | None = None


# --- Market-status authority (replay row + read-only live protocol) -----------------------


class MarketStatusEvent(_FrozenModel):
    """A typed replay/tape market-status row (REQ-027). Same iff-invariant as the observation leg:
    ``recv_ts`` / ``epoch`` are None IFF ``status == "UNKNOWN"``. A tape without status rows yields
    ``UNKNOWN`` (fail closed); the harness may never silently synthesize ``ACTIVE``."""

    venue_market_ref: str
    status: MarketStatus
    recv_ts: int | None
    epoch: int | None

    @model_validator(mode="after")
    def _status_sentinel_iff_unknown(self) -> MarketStatusEvent:
        is_unknown = self.status == "UNKNOWN"
        all_none = self.recv_ts is None and self.epoch is None
        if is_unknown and not all_none:
            raise ValueError("status == UNKNOWN requires recv_ts and epoch to be None")
        if not is_unknown and (self.recv_ts is None or self.epoch is None):
            raise ValueError(
                f"status == {self.status} requires non-None recv_ts and epoch"
            )
        return self


class MarketStatusAuthority(Protocol):
    """Read-only typed authority the (non-ranked) orchestration/adapter layer reads market status
    from (REQ-027). ``read`` returns ``(status, recv_ts, epoch)`` with the SAME iff-invariant:
    the optional fields are None IFF the status is ``UNKNOWN``. A missing / failed / regressed /
    over-age read maps to ``UNKNOWN``; a restart increments the epoch. The core NEVER infers status
    from book shape — status enters only through this provenance-bound read."""

    def read(
        self, venue_market_ref: str
    ) -> tuple[MarketStatus, int | None, int | None]: ...


# --- Deterministic decision / client-order identity ---------------------------------------
# Pure identity helpers (REQ-025 / REQ-095 / AC-022): a decision's identity is a ``sha256`` over
# the SHARED canonical serializer of its provenance inputs, and a leg's client-order id is a
# ``sha256`` over the decision id + leg role. Both are pure functions of their arguments ONLY — NO
# module-level counter, NO wall clock, NO randomness — so an authorized retry with identical inputs
# reproduces a byte-identical id while a distinct observation yields a distinct id. E5-T1 wires
# these into every ``StrategyDecision``; kept as module-level functions so the identity byte
# contract lives with the ``serialize_payload`` authority the rest of this module already uses.


def decision_id(
    strategy_id: str,
    strategy_revision: str,
    config_hash: str,
    session_id: str,
    observation_hash: str,
    prior_state_hash: str,
) -> str:
    """``sha256`` hexdigest binding the six provenance fields into one deterministic id (REQ-025).

    Hashes ``serialize_payload`` of the ordered mapping of these exact inputs (the shared canonical
    serializer sorts keys, so the wire bytes are process-stable). Identity is a pure function of its
    causes — strategy identity, config, session, observation, and prior state — with no counter,
    clock, or randomness, so the same authorized inputs always reproduce the same id (REQ-095).
    """
    canonical = serialize_payload(
        {
            "strategy_id": strategy_id,
            "strategy_revision": strategy_revision,
            "config_hash": config_hash,
            "session_id": session_id,
            "observation_hash": observation_hash,
            "prior_state_hash": prior_state_hash,
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def client_order_id(decision_id: str, leg_role: str) -> str:
    """``sha256`` hexdigest binding a decision id + leg role into one per-leg id (REQ-095).

    Hashes ``serialize_payload`` of ``{"decision_id": …, "leg_role": …}``. Pure and deterministic:
    the same ``(decision_id, leg_role)`` always yields the same client-order id (enabling a stable
    replacement lineage), while a distinct leg role yields a distinct id — no counter or wall clock.
    """
    canonical = serialize_payload({"decision_id": decision_id, "leg_role": leg_role})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Honest evidence labels + pinned strategy identity (HON-001 / HON-003 / REQ-044) -------
# The four mandatory honesty-label VALUES an R4-B run carries, reused VERBATIM from the R4-A dust
# surface (``dust_execution.analysis.REQUIRED_DUST_RUN_LABELS`` + the ``EvidenceClass`` Literal).
# They are RE-DECLARED here as pinned module constants — deliberately NOT imported — because the
# pure tier may not depend on ``dust_execution`` (the E1-T1 four-module import whitelist). The
# working honesty surface is these constants + the shared relabel guard + the rank denylist;
# ``manifest.forbidden_claims`` is INERT prose. R4-B proves SAFETY, not alpha: there is NO code
# path that promotes this evidence class to ``EVIDENCE_GATED`` / ``PROMOTED`` — promotion is a
# Gate B concern, out of R4-B scope (Codex-plan-review MAJOR-5 / AC-003).
EVIDENCE_CLASS: Final[str] = "EXPERIMENTAL_DUST"
RUN_LABEL: Final[str] = "DUST_LIVE"
CALIBRATION_LABEL: Final[str] = "UNCALIBRATED"
EDGE_LABEL: Final[str] = "NOT_PROVEN_EDGE"

# The pinned strategy identity fed as ``strategy_id`` / ``strategy_revision`` into ``decision_id``
# (REQ-044). DISTINCT from the historical raw-FV ``TxLineFairMarketMakerAgent`` agent id so the two
# strategies never share a decision-identity namespace (Codex-plan-review MAJOR-5 attack 1).
# ``config.StrategyConfig.strategy_id`` pins the SAME literal; the two pure modules cannot
# cross-import, so a drift is caught by ``test_strategy_id_matches_config_default`` in the test tier.
STRATEGY_ID: Final[str] = "venue-anchored-txline-guarded-maker"
STRATEGY_REVISION: Final[str] = "r4b-v0"
