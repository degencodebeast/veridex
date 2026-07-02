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

from pathlib import Path
from typing import Literal, TypedDict

from veridex.ingest.txline_client import (
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


def probe_targets(base: str, fixture_id: int | None) -> list[ProbeTarget]:
    """Build the full set of §3.1 probe targets against ``base``.

    Every spec capability appears by NAME regardless of ``fixture_id`` —
    when a fixture id is unknown, the fixture-scoped targets (``odds_updates``,
    ``odds_snapshot``, ``scores_updates``) still appear so the matrix
    documents them, using a placeholder fixture id in their URL.

    Args:
        base: TxLINE API base URL (e.g. ``https://txline-dev.txodds.com/api``).
        fixture_id: A known fixture id to scope fixture-level probes to, or
            ``None`` to use a placeholder (those targets are then skipped by
            :func:`main`, not omitted here).

    Returns:
        Probe targets covering odds/scores streams, fixture-scoped odds
        updates + snapshot + scores updates, odds validation, and all three
        undocumented discovery candidate paths (fixtures/sports/competitions).
    """
    fid = fixture_id if fixture_id is not None else 0
    return [
        {"name": "odds_stream", "url": odds_stream_url(base), "kind": "sse_head"},
        {"name": "odds_updates", "url": odds_updates_url(base, fid), "kind": "get"},
        {"name": "odds_snapshot", "url": f"{base}/odds/snapshot/{fid}", "kind": "get"},
        {"name": "scores_stream", "url": scores_stream_url(base), "kind": "sse_head"},
        {"name": "scores_updates", "url": scores_updates_url(base, fid), "kind": "get"},
        {
            "name": "odds_validation",
            "url": odds_validation_url(base, "PLACEHOLDER"),
            "kind": "get",
        },
        {"name": "fixtures_discovery", "url": f"{base}/fixtures", "kind": "get"},
        {"name": "sports_discovery", "url": f"{base}/sports", "kind": "get"},
        {"name": "competitions_discovery", "url": f"{base}/competitions", "kind": "get"},
    ]


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
    :func:`veridex.config.require_txline`. For each target, does a GET
    (``sse_head`` targets open the stream, read up to 3 lines, then close).
    Never raises on a failed probe — the status/error is captured into the
    :class:`ProbeResult` instead. Writes the rendered markdown to
    ``.omc/research/txline-access-matrix.md`` at the workspace root (i.e.
    one level above the repo root). Never writes secrets (JWT/token) into
    the output.
    """
    import httpx  # noqa: PLC0415

    from veridex.config import get_settings, require_txline
    from veridex.ingest.live_client import build_auth_headers

    settings = get_settings()
    jwt, token = require_txline(settings)
    base = settings.txline_base_url
    headers = build_auth_headers(jwt, token)

    targets = probe_targets(base, fixture_id=None)
    results: list[ProbeResult] = []

    async with httpx.AsyncClient() as client:
        for target in targets:
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
