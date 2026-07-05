"""T1 — TxLINE access-matrix probe (REQ-2D-001 / AC-2D-001).

Operator tool that probes what the free TxLINE API token can actually reach,
so the rest of the Phase 2D sprint can be sized correctly. Emits a §4.6
markdown table documenting HTTP result, payload sanity, coverage, and
fallback for every spec capability — including ones that were skipped or
returned an error, marked honestly rather than silently omitted.

Trust-path module (CON-010): ``httpx`` is imported **lazily** inside
:func:`main` only, so ``import scripts.txline_live.access_matrix`` stays
network-lib-free and the offline test suite never needs live credentials.

Pure functions
--------------
:func:`probe_targets`
    Builds the list of probe targets from the existing URL builders in
    :mod:`veridex.ingest.txline_client`. No I/O.
:func:`render_matrix`
    Renders a list of :class:`ProbeResult` into the §4.6 markdown table.
    Unknown/unprobed cells render the literal ``UNKNOWN`` token.

Async network shell
--------------------
:func:`main`
    Operator entrypoint: loads TxLINE creds, GETs every target (SSE targets
    open the stream, read up to 3 lines, then close), never raises on a
    failed probe, and writes the rendered markdown to
    ``.omc/research/txline-access-matrix.md`` at the workspace root.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, TypedDict

from veridex.ingest.txline_client import (
    fixtures_snapshot_url,
    odds_snapshot_url,
    odds_stream_url,
    odds_updates_url,
    odds_validation_url,
    scores_stream_url,
    scores_updates_url,
)

ProbeKind = Literal["get", "sse_head"]


class ProbeTarget(TypedDict):
    """One capability to probe: a name, the URL to hit, and how to hit it."""

    name: str
    url: str
    kind: ProbeKind


class ProbeResult(ProbeTarget):
    """A :class:`ProbeTarget` plus the observed (or unobserved) outcome."""

    status: int | str
    payload_note: str
    coverage_note: str


_UNKNOWN = "UNKNOWN"
_SKIPPED_NO_FIXTURE_NOTE = "no fixture_id supplied (set TXLINE_PROBE_FIXTURE_ID)"
_SKIPPED_NO_COMPETITION_NOTE = (
    "no competition supplied (set TXLINE_PROBE_COMPETITION_ID + TXLINE_PROBE_START_EPOCH_DAY)"
)
# A PLACEHOLDER messageId 404 is NOT an access signal — validation proofs exist only for SEALED
# records, so this target can only be exercised with a real messageId lifted from a live odds
# update. Its 404 must never be counted as an endpoint failure.
_VALIDATION_PLACEHOLDER_NOTE = "needs a real messageId from a live odds update (placeholder 404 is not an access signal)"
_FIXTURE_SCOPED_NAMES = frozenset({"odds_updates", "odds_snapshot", "scores_updates"})

_MATRIX_HEADERS = (
    "capability",
    "endpoint",
    "HTTP result",
    "payload sanity",
    "coverage",
    "historical depth",
    "rate-limit notes",
    "product use",
    "fallback",
)


def probe_targets(
    base: str,
    fixture_id: int | None,
    *,
    competition_id: int | None = None,
    start_epoch_day: int | None = None,
    as_of: int = 0,
) -> list[ProbeTarget]:
    """Build the full set of §3.1 probe targets against ``base``.

    Every spec capability appears by NAME regardless of the optional
    parameters — when an id/param is unknown a placeholder is used in the
    built URL and :func:`main` records an honest ``SKIPPED`` status (via
    :func:`skip_note_for` + :func:`build_skipped_result`) rather than firing a
    misleading placeholder probe.

    Args:
        base: TxLINE API base URL (e.g. ``https://txline-dev.txodds.com/api``).
        fixture_id: A known fixture id to scope fixture-level probes to, or
            ``None`` for a placeholder fid.
        competition_id: Competition to scope the documented
            ``/fixtures/snapshot`` discovery probe to, or ``None`` for a
            placeholder (the probe is then SKIPPED by :func:`main`).
        start_epoch_day: Epoch-day floor for the discovery snapshot, paired
            with ``competition_id``.
        as_of: Point-in-time (epoch seconds) for the ``odds_snapshot`` probe —
            the bare snapshot is empty pre-match, so ``asOf`` is required
            (CON-040).

    Returns:
        Probe targets covering odds/scores streams, fixture-scoped odds
        updates + point-in-time snapshot + scores updates, odds validation,
        and the ONE documented ``/fixtures/snapshot`` discovery path (the bare
        ``/fixtures`` / ``/sports`` / ``/competitions`` paths all 404 and are
        no longer probed).
    """
    fid = fixture_id if fixture_id is not None else 0
    cid = competition_id if competition_id is not None else 0
    sed = start_epoch_day if start_epoch_day is not None else 0
    return [
        {"name": "odds_stream", "url": odds_stream_url(base), "kind": "sse_head"},
        {"name": "odds_updates", "url": odds_updates_url(base, fid), "kind": "get"},
        {"name": "odds_snapshot", "url": odds_snapshot_url(base, fid, as_of), "kind": "get"},
        {"name": "scores_stream", "url": scores_stream_url(base), "kind": "sse_head"},
        {"name": "scores_updates", "url": scores_updates_url(base, fid), "kind": "get"},
        {
            "name": "odds_validation",
            "url": odds_validation_url(base, "PLACEHOLDER"),
            "kind": "get",
        },
        {"name": "fixtures_discovery", "url": fixtures_snapshot_url(base, cid, sed), "kind": "get"},
    ]


def skip_note_for(
    target: ProbeTarget,
    *,
    fixture_id: int | None,
    competition_id: int | None,
    start_epoch_day: int | None,
) -> str | None:
    """Decide whether a target must be SKIPPED rather than live-probed (pure — no I/O).

    Returns an explanatory note when the target cannot be probed honestly, or ``None`` when it
    should be probed for real. The three skip reasons:

    - ``odds_validation``: ALWAYS skipped — its ``messageId=PLACEHOLDER`` 404 is not an access
      signal (proofs exist only for SEALED records; a real messageId must come from a live update).
    - fixture-scoped targets (``odds_updates``/``odds_snapshot``/``scores_updates``) with no
      ``fixture_id``: a ``fid=0`` 404 would be misread as "endpoint down".
    - ``fixtures_discovery`` with no ``competition_id``/``start_epoch_day``: the documented
      discovery snapshot needs both to return anything meaningful.
    """
    name = target["name"]
    if name == "odds_validation":
        return _VALIDATION_PLACEHOLDER_NOTE
    if name in _FIXTURE_SCOPED_NAMES and fixture_id is None:
        return _SKIPPED_NO_FIXTURE_NOTE
    if name == "fixtures_discovery" and (competition_id is None or start_epoch_day is None):
        return _SKIPPED_NO_COMPETITION_NOTE
    return None


def build_skipped_result(target: ProbeTarget, note: str | None = None) -> ProbeResult:
    """Turn a :class:`ProbeTarget` into an honest ``SKIPPED`` result.

    Used by :func:`main` in place of firing a misleading placeholder probe (a ``fid=0`` /
    ``messageId=PLACEHOLDER`` 404 an operator could misread as "endpoint down"). Pure — no I/O.

    Args:
        target: The :class:`ProbeTarget` being skipped.
        note: The explanatory ``payload_note`` (typically from :func:`skip_note_for`). Defaults to
            the no-fixture-id note for backward compatibility.

    Returns:
        A :class:`ProbeResult` with ``status="SKIPPED"`` and the explanatory ``payload_note``.
    """
    return {
        **target,
        "status": "SKIPPED",
        "payload_note": note if note is not None else _SKIPPED_NO_FIXTURE_NOTE,
        "coverage_note": "",
    }


def render_matrix(results: list[ProbeResult]) -> str:
    """Render probe results as the §4.6 markdown access-matrix table.

    Any cell whose value is empty renders the literal ``UNKNOWN`` token —
    unprobed/unknown facts are never silently blank.

    Args:
        results: Probe outcomes (typically produced by :func:`main`).

    Returns:
        A markdown table string: header row, separator row, one data row
        per result.
    """
    lines = [
        "| " + " | ".join(_MATRIX_HEADERS) + " |",
        "| " + " | ".join("---" for _ in _MATRIX_HEADERS) + " |",
    ]
    for r in results:
        cells = (
            str(r.get("name") or _UNKNOWN),
            str(r.get("url") or _UNKNOWN),
            str(r.get("status") if r.get("status") not in (None, "") else _UNKNOWN),
            str(r.get("payload_note") or _UNKNOWN),
            str(r.get("coverage_note") or _UNKNOWN),
            _UNKNOWN,
            _UNKNOWN,
            _UNKNOWN,
            _UNKNOWN,
        )
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


async def main() -> None:
    """Operator entrypoint: live-probe every target and write the §4.6 matrix.

    Loads TxLINE credentials via :func:`veridex.config.get_settings` +
    :func:`veridex.config.require_txline`. Reads an optional fixture id from
    the ``TXLINE_PROBE_FIXTURE_ID`` env var (coerced to ``int`` when set) to
    scope the three fixture-level targets. When that env var is absent, those
    three targets are NOT probed with a misleading fid=0 placeholder — they
    are recorded ``SKIPPED`` via :func:`build_skipped_result` instead. For
    every other target, does a GET (``sse_head`` targets open the stream,
    read up to 3 lines, then close). Never raises on a failed probe — the
    status/error is captured into the :class:`ProbeResult` instead. Writes
    the rendered markdown to ``.omc/research/txline-access-matrix.md`` at
    the workspace root (i.e. one level above the repo root). Never writes
    secrets (JWT/token) into the output.
    """
    import time  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from veridex.config import get_settings, require_txline
    from veridex.ingest.live_client import build_auth_headers

    settings = get_settings()
    jwt, token = require_txline(settings)
    base = settings.txline_base_url
    headers = build_auth_headers(jwt, token)

    fixture_id_env = os.environ.get("TXLINE_PROBE_FIXTURE_ID")
    fixture_id = int(fixture_id_env) if fixture_id_env else None

    competition_env = os.environ.get("TXLINE_PROBE_COMPETITION_ID", "72")  # documented default competition
    competition_id = int(competition_env) if competition_env else None
    start_epoch_env = os.environ.get("TXLINE_PROBE_START_EPOCH_DAY")
    start_epoch_day = int(start_epoch_env) if start_epoch_env else None
    as_of_env = os.environ.get("TXLINE_PROBE_ASOF")
    as_of = int(as_of_env) if as_of_env else int(time.time())

    targets = probe_targets(
        base,
        fixture_id,
        competition_id=competition_id,
        start_epoch_day=start_epoch_day,
        as_of=as_of,
    )
    results: list[ProbeResult] = []

    async with httpx.AsyncClient() as client:
        for target in targets:
            skip_note = skip_note_for(
                target,
                fixture_id=fixture_id,
                competition_id=competition_id,
                start_epoch_day=start_epoch_day,
            )
            if skip_note is not None:
                results.append(build_skipped_result(target, skip_note))
                continue
            result: ProbeResult = {**target, "status": _UNKNOWN, "payload_note": "", "coverage_note": ""}
            try:
                if target["kind"] == "sse_head":
                    async with client.stream("GET", target["url"], headers=headers) as resp:
                        result["status"] = resp.status_code
                        lines_seen = 0
                        async for _line in resp.aiter_lines():
                            lines_seen += 1
                            if lines_seen >= 3:
                                break
                        result["payload_note"] = f"{lines_seen} SSE line(s) read"
                else:
                    resp = await client.get(target["url"], headers=headers)
                    result["status"] = resp.status_code
                    body = resp.text
                    result["payload_note"] = f"{len(body)} byte(s)"
            except Exception as e:  # noqa: BLE001 — honest capture, probe must never raise
                result["status"] = f"ERROR: {type(e).__name__}"
                result["payload_note"] = str(e)[:200]
            results.append(result)

    matrix = render_matrix(results)
    # __file__ = <repo>/scripts/txline_live/access_matrix.py; parents[3] = workspace root
    # (one level above the veridex-arena repo) — the canonical .omc/ with research/plans/
    # reviews lives there. The repo-local .omc/ (parents[2]) is a separate, git-ignored
    # dir and is NOT the target.
    out_path = Path(__file__).resolve().parents[3] / ".omc" / "research" / "txline-access-matrix.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(matrix)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
