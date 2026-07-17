"""R-0a — offline-testable live-ingestion capture chain + PROVENANCE HONESTY (LOAD-BEARING).

Wires the EXISTING ingestion components into ONE tested chain:

    capture source (creds) -> /odds/stream -> parse_sse_line -> recorder session
        -> pack_from_session -> a content-hashed ReplayPack

Reused (not rewritten) here: :func:`veridex.ingest.marketstate.parse_sse_line`, the recorder
session format (:class:`~veridex.ingest.recorder.SessionMeta`,
:func:`~veridex.ingest.recorder.envelope_line`, :func:`~veridex.ingest.recorder.finalize_meta`),
:func:`veridex.ingest.replay_pack.pack_from_session`, and
:func:`veridex.ingest.live_client.build_auth_headers`.

PROVENANCE HONESTY (the trust boundary, MAJOR-1) — a pack's authority (provenance, test_capture,
synthetic, evidence rung, capture method) is derived from a CLOSED set of controller-owned producers
(:func:`_authority_for_source`), NEVER copied from a caller/source-supplied string, and it is folded
into the pack's VERSIONED ``content_hash`` (pack_version 2) so a post-build relabel is refused by
:func:`~veridex.ingest.replay_pack.verify_content_hash`. Only the concrete :class:`LiveCaptureSource`
(real creds via ``require_live_creds``, fail-closed) maps to :data:`GENUINE_TXLINE_PROVENANCE`; every
other/unknown/custom source maps to :data:`TEST_FAKE_PROVENANCE`. :func:`run_capture_chain` has NO
provenance parameter. A fake-backed OR arbitrary-string-declaring run therefore can NEVER mint a
"genuine TxLINE" pack — the exact failure this task prevents (a demo passing a fake pack off as live
TxLINE). :func:`is_genuine_pack` requires hash-verification FIRST, then a coherent genuine state, and
a legacy v1 pack (authority not hash-bound) can never read genuine.

Trust-path module (``ingest/`` is import-audited): NO LLM SDK imports; ``httpx`` is imported
lazily inside :meth:`LiveCaptureSource.stream_client` only (CON-010 async-shell split).
"""

from __future__ import annotations

import json
import time
from dataclasses import InitVar, dataclass
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from veridex.ingest.feed_health import DEFAULT_STALE_AFTER_S, FeedState, derive_feed_state
from veridex.ingest.live_client import build_auth_headers
from veridex.ingest.marketstate import parse_sse_line
from veridex.ingest.recorder import SessionMeta, envelope_line, finalize_meta
from veridex.ingest.replay_pack import ReplayPack, pack_from_session, verify_content_hash
from veridex.provenance import EvidenceRung

#: Positive provenance for a pack captured from the REAL TxLINE feed. Reachable ONLY through the
#: CLOSED producer set (:func:`_authority_for_source` / the genuine authority builders); no parameter
#: and no source-supplied string lets any other path mint it.
GENUINE_TXLINE_PROVENANCE = "genuine-txline"
#: Provenance a recording-fake / unknown capture is assigned — a TEST pack, never a genuine capture.
TEST_FAKE_PROVENANCE = "test-fake-recording"
#: Provenance for SYNTHETIC illustrative odds (the shipped demo tape) — visibly non-genuine.
SYNTHETIC_PROVENANCE = "synthetic-illustrative"
#: Fail-safe label for a pack whose ``capture`` block declares NO provenance: we can never assert
#: it was genuinely captured, so it reads "unknown" — an unmarked pack NEVER means genuine.
UNKNOWN_PROVENANCE = "unknown-provenance"

#: Capture-method labels — how a pack's records were obtained. The two GENUINE methods are the only
#: ones an ``is_genuine_pack`` pack may carry; the others are honest non-genuine markers.
LIVE_SSE_CAPTURE_METHOD = "live-sse-stream"
BACKFILL_CAPTURE_METHOD = "odds-updates-backfill"
SYNTHETIC_CAPTURE_METHOD = "synthetic-tape"
RECORDING_FAKE_CAPTURE_METHOD = "recording-fake"

#: The ONLY evidence rungs / capture methods a genuine-TxLINE pack may carry (both distinct, honest
#: genuine paths: a live SSE recording vs a verified REST backfill). Any other value fails-safe.
_GENUINE_EVIDENCE_RUNGS: frozenset[str] = frozenset(
    {EvidenceRung.RECORDED_LIVE_QUOTE.value, EvidenceRung.BACKFILLED_PRICE_HISTORY.value}
)
_GENUINE_CAPTURE_METHODS: frozenset[str] = frozenset({LIVE_SSE_CAPTURE_METHOD, BACKFILL_CAPTURE_METHOD})

#: Default TxLINE odds SSE endpoint path appended to the base URL.
_ODDS_STREAM_PATH = "/odds/stream"

#: Module-private capability SEAL (D-residual). Only the CLOSED producer paths in THIS module hold it,
#: so only they can mint a GENUINE :class:`PackAuthority`. It is deliberately not part of the public
#: API surface — reaching for it is equivalent to editing trusted application code, which is outside
#: the honesty threat model (a public caller cannot obtain it).
_AUTHORITY_SEAL: Final[object] = object()


@dataclass(frozen=True)
class PackAuthority:
    """A SEALED provenance capability: the five authority fields a v2 pack binds (MAJOR-1 honesty core).

    The D-residual fix. A GENUINE capability — one carrying ``provenance == GENUINE_TXLINE_PROVENANCE``
    OR a genuine ``evidence_rung`` / ``capture_method`` — can be constructed ONLY by the CLOSED producer
    paths in this module (:func:`_authority_for_source` for live capture, :func:`_genuine_backfill_authority`
    for the verified-backfill banker), which pass the module-private :data:`_AUTHORITY_SEAL`. Any other
    attempt to construct a genuine capability raises. Non-genuine capabilities (synthetic / test /
    unknown) need no seal. So an arbitrary public caller cannot fabricate a genuine capability to hand to
    :func:`~veridex.ingest.replay_pack.pack_from_session`: the ordinary public builder can NEVER mint a
    genuine pack from arbitrary records. This is application-level honesty (a closed set of trusted
    producers), NOT signed-origin crypto — the cryptographic-attestation upgrade stays post-hackathon.
    """

    provenance: str
    test_capture: bool
    synthetic: bool
    evidence_rung: str | None
    capture_method: str
    seal: InitVar[object | None] = None

    def __post_init__(self, seal: object | None) -> None:
        # Store the SEAL OBJECT ITSELF (`_seal_token`), never a derived boolean (D-residual, second
        # Codex re-review). A boolean is a VALUE an attacker can assert via `object.__setattr__` on an
        # `object.__new__`-forged instance (`object.__setattr__(f, "_sealed", True)` — same primitive
        # that forges the five genuine fields, one more line). The write-boundary guard
        # (:func:`_assert_authority_mintable`) instead compares `_seal_token` by IDENTITY against the
        # module-private :data:`_AUTHORITY_SEAL` sentinel — an attacker can set `_seal_token` to some
        # value, but never to the REAL sentinel object without importing the underscore-private module
        # global (out-of-scope internals abuse, same exclusion that already makes attack-E — a fake
        # `seal=object()` — unforgeable).
        if self._claims_genuine() and seal is not _AUTHORITY_SEAL:
            raise PermissionError(
                "a genuine PackAuthority can be minted only by a closed producer path "
                "(live capture or the verified-backfill banker), never via the public API"
            )
        object.__setattr__(self, "_seal_token", seal)

    def _claims_genuine(self) -> bool:
        """True if ANY field carries a genuine marker — the whole capability is then seal-gated.

        NOTE: this is a construction-time, DEFENSE-IN-DEPTH check only. The AUTHORITATIVE enforcement
        is :func:`_assert_authority_mintable`, called from the actual write boundary
        (:func:`~veridex.ingest.replay_pack.pack_from_session`) — construction-time checks alone are
        bypassable (``object.__new__`` skips ``__post_init__`` entirely; a hostile subclass could
        override this very method to always return ``False``).
        """
        return (
            self.provenance == GENUINE_TXLINE_PROVENANCE
            or str(self.evidence_rung) in _GENUINE_EVIDENCE_RUNGS
            or self.capture_method in _GENUINE_CAPTURE_METHODS
        )

    def as_capture_fields(self) -> dict[str, Any]:
        """The five authority fields as a plain dict for the pack ``capture`` block / v2 hash."""
        return {
            "provenance": self.provenance,
            "test_capture": self.test_capture,
            "synthetic": self.synthetic,
            "evidence_rung": self.evidence_rung,
            "capture_method": self.capture_method,
        }


def _assert_authority_mintable(authority: PackAuthority) -> None:
    """WRITE-BOUNDARY guard (D-residual, Codex re-review x2) — the ACTUAL enforcement point for the
    genuine-provenance seal, called from :func:`~veridex.ingest.replay_pack.pack_from_session` (the
    real mint point) rather than trusting :class:`PackAuthority` construction alone. FOUR proven
    bypasses of a construction-only / boolean-flag gate, closed together here:

    1. **Duck-typed object**: an arbitrary object exposing an ``as_capture_fields()`` method (never a
       :class:`PackAuthority` at all) skips ``__post_init__`` entirely.
    2. **``object.__new__(PackAuthority)``**: constructs a real ``PackAuthority`` instance WITHOUT
       calling ``__init__``/``__post_init__``, so the seal check never runs; genuine field values can
       then be forced onto it via ``object.__setattr__`` (frozen dataclasses only block the *normal*
       ``__setattr__`` path).
    3. **A ``PackAuthority`` subclass overriding ``_claims_genuine``** to always return ``False``,
       neutering the construction-time check while still carrying genuine field values.
    4. **Forging a boolean "proven seal" flag** (the first fix's own residual): a boolean value is
       something an attacker can ALSO set via the SAME ``object.__setattr__`` primitive that forges
       (2) — ``object.__setattr__(forged, "_sealed", True)`` defeated a flag-based check outright.

    TWO checks together close all four (mirrors the F-residual fix: exact-type membership, not
    ``isinstance``):

    * EXACT type membership — ``type(authority) is PackAuthority`` — rejects the duck-typed object
      (1) AND any subclass (3), since ``type(subclass_instance) is PackAuthority`` is always ``False``.
    * Once exact-type is confirmed (so ``_claims_genuine`` CANNOT be an override), a genuine claim
      requires the stored ``_seal_token`` to be the module-private :data:`_AUTHORITY_SEAL` sentinel BY
      IDENTITY (``is``) — never a boolean. ``_seal_token`` is set ONLY inside ``__post_init__`` to
      WHATEVER ``seal`` value was passed (absent on an ``object.__new__`` instance, since
      ``__post_init__`` never ran). An attacker can forge ``_seal_token`` to *some* value via
      ``object.__setattr__`` (2), but can never forge it to the REAL ``_AUTHORITY_SEAL`` object without
      importing that underscore-private module global — out-of-scope internals abuse, the same
      exclusion that already makes a fake ``seal=object()`` construction-time argument unforgeable (4
      is now closed by the identity check, not a re-derivable boolean).

    Raises:
        TypeError: If ``authority`` is not an exact :class:`PackAuthority` instance.
        PermissionError: If ``authority`` claims a genuine marker but its ``_seal_token`` is not the
            real, module-private ``_AUTHORITY_SEAL`` object by identity.
    """
    if type(authority) is not PackAuthority:
        raise TypeError(
            "pack_from_session authority must be an exact veridex.ingest.capture_chain.PackAuthority "
            f"instance, got {type(authority).__name__!r} — duck-typed and subclassed authority "
            "objects are refused"
        )
    if authority._claims_genuine() and getattr(authority, "_seal_token", None) is not _AUTHORITY_SEAL:
        raise PermissionError(
            "authority claims a genuine marker but was not minted through a closed, sealed producer "
            "path (live capture or the verified-backfill banker) — refusing to emit a genuine-claiming pack"
        )


def _genuine_live_sse_authority() -> PackAuthority:
    """SEALED authority for a GENUINE pack captured from the live ``/odds/stream`` SSE feed.

    CLOSED producer — NOT on the public API surface (D-residual): only :func:`_authority_for_source`
    reaches it, and it passes the module-private seal. Its distinct ``recorded-live-quote`` rung
    separates it from the verified-backfill genuine path.
    """
    return PackAuthority(
        provenance=GENUINE_TXLINE_PROVENANCE,
        test_capture=False,
        synthetic=False,
        evidence_rung=EvidenceRung.RECORDED_LIVE_QUOTE.value,
        capture_method=LIVE_SSE_CAPTURE_METHOD,
        seal=_AUTHORITY_SEAL,
    )


def _genuine_backfill_authority() -> PackAuthority:
    """SEALED authority for a GENUINE pack curated from the verified ``/odds/updates`` backfill.

    CLOSED producer — NOT on the public API surface (D-residual): the verified-backfill banker
    (``scripts/fixtures/build_demo_pack_real.py``) is the only owned caller, and this constructs the
    genuine capability with the module-private seal. Its distinct ``backfilled-price-history`` rung
    separates it from the live-SSE genuine path.
    """
    return PackAuthority(
        provenance=GENUINE_TXLINE_PROVENANCE,
        test_capture=False,
        synthetic=False,
        evidence_rung=EvidenceRung.BACKFILLED_PRICE_HISTORY.value,
        capture_method=BACKFILL_CAPTURE_METHOD,
        seal=_AUTHORITY_SEAL,
    )


def synthetic_authority() -> PackAuthority:
    """Public capability for a SYNTHETIC illustrative pack (the shipped demo tape) — non-genuine, unsealed."""
    return PackAuthority(
        provenance=SYNTHETIC_PROVENANCE,
        test_capture=False,
        synthetic=True,
        evidence_rung=EvidenceRung.SYNTHETIC.value,
        capture_method=SYNTHETIC_CAPTURE_METHOD,
    )


def test_fake_authority() -> PackAuthority:
    """Public capability for a recording-fake / unknown-source capture — a TEST pack, non-genuine, unsealed."""
    return PackAuthority(
        provenance=TEST_FAKE_PROVENANCE,
        test_capture=True,
        synthetic=False,
        evidence_rung=None,
        capture_method=RECORDING_FAKE_CAPTURE_METHOD,
    )


def _scrub(text: str, *secrets: str) -> str:
    """Redact each secret VALUE from *text* before it is printed/written.

    Mirrors ``veridex.live_recorder.sources._scrub`` / the maker ``_scrub_token`` copies — the raw
    values are scrubbed (not trusting an exception's provenance), so a credential embedded in an
    error surfacing from OUTSIDE this module is still redacted.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


@runtime_checkable
class CaptureSource(Protocol):
    """A source of TxLINE capture I/O — an OPEN protocol of just the I/O seam (creds + stream).

    Deliberately carries NO ``provenance`` (MAJOR-1): a source can no longer DECLARE its own
    authority, so a custom/hostile implementation cannot mint genuine by exposing a magic string.
    Authority is derived structurally from a CLOSED set of controller-owned producer types in
    :func:`_authority_for_source` — never from anything the source says about itself.
    """

    def credentials(self) -> tuple[str, str]:
        """Return ``(jwt, api_token)`` for the stream, or FAIL CLOSED (raise) if unavailable."""
        ...

    def stream_client(self) -> Any:
        """Return an httpx-like client supporting ``client.stream("GET", url, headers=...)``."""
        ...


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of one capture-chain run.

    ``pack`` is ``None`` when NO odds records were captured (e.g. a heartbeat-only stream): a
    heartbeat proves liveness but can never mint a market-data pack.
    """

    pack: ReplayPack | None
    pack_dir: Path | None
    provenance: str
    feed_state: FeedState
    odds_records: int
    heartbeats: int


class LiveCaptureSource:
    """The REAL TxLINE capture seam — the ONLY concrete producer that maps to genuine authority.

    It declares NO ``provenance`` of its own (MAJOR-1); its genuine authority is assigned by
    :func:`_authority_for_source` purely from its CONCRETE TYPE, so a look-alike that merely exposes
    a ``provenance`` attribute cannot impersonate it. Credentials come from
    :func:`veridex.live_recorder.sources.require_live_creds` over the process environment
    (``JWT`` + ``TXLINE_X_API_TOKEN``); the guard raises BEFORE any network I/O when either is
    absent. R-0b drives this live path; R-0a only builds/wires it.
    """

    def __init__(self, env: Any = None) -> None:
        import os

        self._env = os.environ if env is None else env

    def credentials(self) -> tuple[str, str]:
        """Resolve real creds fail-closed via ``require_live_creds`` (never weakened)."""
        from veridex.live_recorder.sources import require_live_creds

        return require_live_creds(self._env)

    def stream_client(self) -> Any:
        """A real ``httpx.AsyncClient`` (lazy import — keeps module load network-lib-free)."""
        import httpx  # noqa: PLC0415

        # SSE is long-lived + idle-tolerant: keep connect/write timeouts but DISABLE the read
        # timeout, else a >5s gap between odds ticks trips a spurious disconnect (see live_client).
        return httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))


def _authority_for_source(source: CaptureSource) -> PackAuthority:
    """Derive a pack's authority from a CLOSED set of controller-owned producer TYPES (MAJOR-1 / F-residual).

    This is the honesty core: authority is keyed off the concrete producer, NEVER off anything the
    source declares about itself. EXACT-type membership (``type(source) is LiveCaptureSource``), NOT
    ``isinstance`` — a fake SUBCLASS that overrides ``credentials`` / ``stream_client`` with canned data
    must NOT inherit genuine authority (the F-residual bypass). Only the concrete :class:`LiveCaptureSource`
    maps to the genuine live-SSE capability; every other/unknown/custom/subclassed source — including one
    that hardcodes a ``provenance='genuine-txline'`` attribute — maps to the fail-safe test/unknown
    capability. A future legitimate producer must be given an EXPLICIT reviewed authority mapping here,
    never inherit genuine by accident.
    """
    if type(source) is LiveCaptureSource:
        return _genuine_live_sse_authority()
    return test_fake_authority()


def read_pack_provenance(pack_dir: Path) -> str:
    """Read a pack's SELF-DECLARED provenance from its ``capture`` block (fail-safe).

    A missing/empty/corrupt provenance reads :data:`UNKNOWN_PROVENANCE` — an unmarked pack NEVER
    reads as genuine.
    """
    try:
        capture = json.loads((pack_dir / "pack.json").read_text()).get("capture", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return UNKNOWN_PROVENANCE
    provenance = str(capture.get("provenance", "")).strip()
    return provenance or UNKNOWN_PROVENANCE


def is_genuine_pack(pack_dir: Path) -> bool:
    """True ONLY for a hash-verified v2 pack whose authority is a COHERENT genuine state (MAJOR-1).

    Fail-safe on every gate, in order:

    1. ``pack_version >= 2`` — a legacy v1 pack's authority is NOT hash-bound, so it can never read
       genuine (its provenance could be relabeled without changing its data-only hash);
    2. :func:`~veridex.ingest.replay_pack.verify_content_hash` passes — the authority declaration is
       intact and unchanged since it was hashed (a post-build relabel fails here);
    3. a coherent genuine state — ``provenance == genuine-txline``, ``test_capture is False``,
       ``synthetic is False`` (an OMITTED ``synthetic`` fails closed), an allowed genuine
       ``evidence_rung`` AND ``capture_method``.

    ANY missing, contradictory, or unrecognized field → False.
    """
    try:
        manifest = json.loads((pack_dir / "pack.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    if int(manifest.get("pack_version", 1)) < 2:
        return False  # v1 authority is not hash-bound -> never genuine (fail-safe)
    if not verify_content_hash(pack_dir):
        return False  # hash (incl. the authority block) must verify FIRST
    capture = manifest.get("capture", {})
    return (
        str(capture.get("provenance", "")).strip() == GENUINE_TXLINE_PROVENANCE
        and capture.get("test_capture") is False
        # E-residual: require an EXPLICIT ``synthetic is False`` — a MISSING (None) value must fail
        # closed (matches this function's documented "ANY missing field -> False" rule).
        and capture.get("synthetic") is False
        and str(capture.get("evidence_rung", "")) in _GENUINE_EVIDENCE_RUNGS
        and str(capture.get("capture_method", "")) in _GENUINE_CAPTURE_METHODS
    )


async def run_capture_chain(
    source: CaptureSource,
    *,
    session_dir: Path,
    out_dir: Path,
    base_url: str = "https://txline-dev.txodds.com/api",
    tool_version: str = "capture_chain/1",
    stale_after_s: int = DEFAULT_STALE_AFTER_S,
) -> CaptureResult:
    """Run the full ingest→normalize→record→pack chain over *source*, returning a CaptureResult.

    NOTE the DELIBERATE absence of a ``provenance`` parameter: authority is derived from the CLOSED
    producer set (:func:`_authority_for_source`) purely by the source's concrete TYPE and folded into
    the pack's v2 ``content_hash``. This is the honesty invariant — neither a caller nor a source can
    inject "genuine" for a fake/unknown producer.

    Args:
        source: The capture seam (live or recording-fake). Only :class:`LiveCaptureSource` maps to
            genuine authority; every other source maps to the fail-safe test/unknown authority.
        session_dir: Directory to write the intermediate recorder session into.
        out_dir: Directory to write the produced ReplayPack into.
        base_url: TxLINE API base URL; ``/odds/stream`` is appended.
        tool_version: Recorded ``tool_version`` for the session meta.
        stale_after_s: Staleness budget passed to :func:`~veridex.ingest.feed_health.derive_feed_state`.

    Returns:
        A :class:`CaptureResult`. ``pack``/``pack_dir`` are ``None`` when no odds records were
        captured (a heartbeat-only stream mints no market-data pack).
    """
    # Authority is derived from the CLOSED producer set BY TYPE, never from the source's own words.
    authority = _authority_for_source(source)
    provenance = str(authority.provenance)
    jwt, token = source.credentials()  # fail-closed for the live source — raises BEFORE any I/O
    headers = build_auth_headers(jwt, token)
    url = f"{base_url}{_ODDS_STREAM_PATH}"

    session_dir.mkdir(parents=True, exist_ok=True)
    started_ts = int(time.time())
    start_meta = SessionMeta(started_ts=started_ts, endpoints=[_ODDS_STREAM_PATH], tool_version=tool_version)
    (session_dir / "meta.json").write_text(start_meta.model_dump_json())

    odds_records = 0
    heartbeats = 0
    record_counts: dict[str, int] = {}
    last_frame_ts = started_ts

    client = source.stream_client()
    try:
        with (session_dir / "records.jsonl").open("a") as fh:
            async with client.stream("GET", url, headers=headers) as resp:
                # Scrubbed connect diagnostic — the raw creds are ALWAYS redacted, never logged.
                status = getattr(resp, "status_code", "?")
                print(_scrub(f"[capture] {url} connected: HTTP {status}", jwt, token))
                async for line in resp.aiter_lines():
                    record = parse_sse_line(line)
                    if record is not None:
                        received_ts = int(time.time())
                        last_frame_ts = received_ts
                        fh.write(envelope_line(record, received_ts) + "\n")
                        odds_records += 1
                        fid = record.get("FixtureId")
                        if fid is not None:
                            record_counts[str(fid)] = record_counts.get(str(fid), 0) + 1
                    elif line is not None and line.strip().startswith(":"):
                        # A heartbeat proves liveness but carries no market data.
                        heartbeats += 1
                        last_frame_ts = int(time.time())
    finally:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()

    (session_dir / "meta.json").write_text(
        finalize_meta(start_meta, ended_ts=last_frame_ts, record_counts=record_counts).model_dump_json()
    )

    feed_state = derive_feed_state(
        connecting=False,
        connected=True,
        odds_records_seen=odds_records,
        heartbeats_seen=heartbeats,
        last_frame_ts=last_frame_ts,
        now_ts=last_frame_ts,
        stale_after_s=stale_after_s,
    )

    # Heartbeat-only (or empty) stream: NO odds records -> mint NO pack. A heartbeat cannot make a
    # market-data pack, so we never build one that would then need a misleading provenance.
    if odds_records == 0:
        return CaptureResult(
            pack=None,
            pack_dir=None,
            provenance=provenance,
            feed_state=feed_state,
            odds_records=odds_records,
            heartbeats=heartbeats,
        )

    # Build the pack WITH the closed-set authority folded into its v2 content_hash — a fake seam
    # therefore produces a TEST pack whose test authority is hash-bound, and no parameter or
    # source-declared string can override it.
    pack = pack_from_session(session_dir, out_dir, authority=authority)
    return CaptureResult(
        pack=pack,
        pack_dir=out_dir,
        provenance=provenance,
        feed_state=feed_state,
        odds_records=odds_records,
        heartbeats=heartbeats,
    )
