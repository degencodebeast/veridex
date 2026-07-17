"""R-0a — offline-testable live-ingestion capture chain + PROVENANCE HONESTY (LOAD-BEARING).

Wires the EXISTING ingestion components into ONE tested chain:

    capture source (creds) -> /odds/stream -> parse_sse_line -> recorder session
        -> pack_from_session -> a content-hashed ReplayPack

Reused (not rewritten) here: :func:`veridex.ingest.marketstate.parse_sse_line`, the recorder
session format (:class:`~veridex.ingest.recorder.SessionMeta`,
:func:`~veridex.ingest.recorder.envelope_line`, :func:`~veridex.ingest.recorder.finalize_meta`),
:func:`veridex.ingest.replay_pack.pack_from_session`, and
:func:`veridex.ingest.live_client.build_auth_headers`.

PROVENANCE HONESTY (the trust boundary) — a pack's provenance is stamped from the capture
SOURCE's OWN declared provenance, and :func:`run_capture_chain` has NO provenance parameter.
Only :class:`LiveCaptureSource` (real creds via ``require_live_creds``, fail-closed) declares
:data:`GENUINE_TXLINE_PROVENANCE`; any recording-fake source declares
:data:`TEST_FAKE_PROVENANCE`. A fake-backed run therefore can NEVER mint a "genuine TxLINE"
pack — the exact failure this task prevents (a demo passing a fake pack off as live TxLINE).

Trust-path module (``ingest/`` is import-audited): NO LLM SDK imports; ``httpx`` is imported
lazily inside :meth:`LiveCaptureSource.stream_client` only (CON-010 async-shell split).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from veridex.ingest.feed_health import DEFAULT_STALE_AFTER_S, FeedState, derive_feed_state
from veridex.ingest.live_client import build_auth_headers
from veridex.ingest.marketstate import parse_sse_line
from veridex.ingest.recorder import SessionMeta, envelope_line, finalize_meta
from veridex.ingest.replay_pack import ReplayPack, pack_from_session

#: Positive provenance for a pack captured from the REAL TxLINE feed. Reachable ONLY through
#: :attr:`LiveCaptureSource.provenance`; there is no parameter that lets any other path mint it.
GENUINE_TXLINE_PROVENANCE = "genuine-txline"
#: Provenance a recording-fake capture self-declares — a TEST pack, never a genuine feed capture.
TEST_FAKE_PROVENANCE = "test-fake-recording"
#: Fail-safe label for a pack whose ``capture`` block declares NO provenance: we can never assert
#: it was genuinely captured, so it reads "unknown" — an unmarked pack NEVER means genuine.
UNKNOWN_PROVENANCE = "unknown-provenance"

#: Default TxLINE odds SSE endpoint path appended to the base URL.
_ODDS_STREAM_PATH = "/odds/stream"


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
    """A source of TxLINE capture I/O whose ``provenance`` is fixed to its own identity.

    ``provenance`` is a read-only property (not a constructor argument) — this is what makes
    the honesty invariant structural: a source cannot be *asked* to lie about its provenance.
    """

    @property
    def provenance(self) -> str:
        """This source's fixed provenance label (genuine vs test)."""
        ...

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
    """The REAL TxLINE capture seam — genuine provenance, creds resolved FAIL-CLOSED.

    Credentials come from :func:`veridex.live_recorder.sources.require_live_creds` over the
    process environment (``JWT`` + ``TXLINE_X_API_TOKEN``); the guard raises BEFORE any network
    I/O when either is absent. R-0b drives this live path; R-0a only builds/wires it.
    """

    def __init__(self, env: Any = None) -> None:
        import os

        self._env = os.environ if env is None else env

    @property
    def provenance(self) -> str:
        """Always :data:`GENUINE_TXLINE_PROVENANCE` — the ONLY source that declares it."""
        return GENUINE_TXLINE_PROVENANCE

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


def stamp_pack_provenance(pack_dir: Path, provenance: str, *, test_capture: bool) -> None:
    """Stamp ``provenance`` + a ``test_capture`` flag INTO the pack's ``capture`` block.

    Mirrors ``scripts/demo_phase2d.py``: the markers ride in ``capture`` metadata, which
    ``content_hash`` does NOT cover (it hashes the DATA files), so the pack's ``content_hash`` is
    unchanged. Provenance therefore travels WITH the pack and can never be separated from it.
    """
    pack_path = pack_dir / "pack.json"
    pack_doc = json.loads(pack_path.read_text())
    pack_doc["capture"]["provenance"] = provenance
    pack_doc["capture"]["test_capture"] = bool(test_capture)
    pack_path.write_text(json.dumps(pack_doc))


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
    """True ONLY for a pack positively stamped genuine AND not flagged ``test_capture``.

    Fail-safe: an unmarked pack, or one flagged ``test_capture``, is never genuine.
    """
    try:
        capture = json.loads((pack_dir / "pack.json").read_text()).get("capture", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    if capture.get("test_capture") is True:
        return False
    return str(capture.get("provenance", "")).strip() == GENUINE_TXLINE_PROVENANCE


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

    NOTE the DELIBERATE absence of a ``provenance`` parameter: provenance is read solely from
    ``source.provenance`` and stamped into the pack. This is the honesty invariant — a caller
    cannot inject "genuine" for a fake source.

    Args:
        source: The capture seam (live or recording-fake). Its ``provenance`` is stamped as-is.
        session_dir: Directory to write the intermediate recorder session into.
        out_dir: Directory to write the produced ReplayPack into.
        base_url: TxLINE API base URL; ``/odds/stream`` is appended.
        tool_version: Recorded ``tool_version`` for the session meta.
        stale_after_s: Staleness budget passed to :func:`~veridex.ingest.feed_health.derive_feed_state`.

    Returns:
        A :class:`CaptureResult`. ``pack``/``pack_dir`` are ``None`` when no odds records were
        captured (a heartbeat-only stream mints no market-data pack).
    """
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
            provenance=source.provenance,
            feed_state=feed_state,
            odds_records=odds_records,
            heartbeats=heartbeats,
        )

    pack = pack_from_session(session_dir, out_dir)
    # Stamp provenance FROM the source's fixed identity — a fake seam therefore stamps TEST, never
    # genuine, and no parameter can override it.
    stamp_pack_provenance(out_dir, source.provenance, test_capture=source.provenance != GENUINE_TXLINE_PROVENANCE)
    return CaptureResult(
        pack=pack,
        pack_dir=out_dir,
        provenance=source.provenance,
        feed_state=feed_state,
        odds_records=odds_records,
        heartbeats=heartbeats,
    )
