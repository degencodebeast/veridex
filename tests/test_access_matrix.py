"""T1 — TxLINE access-matrix probe tests (REQ-2D-001 / AC-2D-001).

TDD Iron Law: written RED (ImportError — module missing) before the
production module existed.

Behaviors under test
---------------------
- ``probe_targets``: PURE target builder covering every spec capability
  (odds stream/updates/snapshot, scores stream/updates, odds validation,
  fixtures/sports/competitions discovery), regardless of whether
  ``fixture_id`` is known — all 9 capability names are always present.
- ``render_matrix``: PURE §4.6 markdown table renderer; unknown/unprobed
  cells render the literal ``UNKNOWN`` token (or an explicit ``SKIPPED``
  status), never silently blank; empty input renders header+separator only.
- ``build_skipped_result``: PURE — turns a fixture-scoped :class:`ProbeTarget`
  into an honest ``SKIPPED`` :class:`ProbeResult` when no fixture id is
  supplied, instead of a misleading fid=0 probe.

All three are offline-only — no network, no real creds. ``main()`` is the
only live-touching code in ``access_matrix.py`` and is not exercised here.
"""

from __future__ import annotations

from scripts.txline_live.access_matrix import build_skipped_result, probe_targets, render_matrix


def test_probe_targets_cover_all_spec_capabilities():
    names = {t["name"] for t in probe_targets("https://x/api", fixture_id=123)}
    assert {
        "odds_stream",
        "odds_updates",
        "odds_snapshot",
        "scores_stream",
        "scores_updates",
        "odds_validation",
        "fixtures_discovery",
    } <= names


def test_render_matrix_marks_unknowns_explicitly():
    row = {
        "name": "scores_stream",
        "url": "u",
        "kind": "sse_head",
        "status": "SKIPPED",
        "payload_note": "",
        "coverage_note": "",
    }
    out = render_matrix([row])
    assert "UNKNOWN" in out or "SKIPPED" in out
    assert "| scores_stream |" in out


def test_probe_targets_returns_all_nine_capabilities_without_fixture_id():
    names = {t["name"] for t in probe_targets("https://x/api", fixture_id=None)}
    assert names == {
        "odds_stream",
        "odds_updates",
        "odds_snapshot",
        "scores_stream",
        "scores_updates",
        "odds_validation",
        "fixtures_discovery",
        "sports_discovery",
        "competitions_discovery",
    }


def test_render_matrix_empty_input_returns_header_only():
    out = render_matrix([])
    lines = [line for line in out.split("\n") if line]
    assert len(lines) == 2
    assert lines[0].startswith("| capability |")
    assert lines[1].startswith("| ---")


def test_fixture_scoped_targets_render_skipped_without_fixture_id():
    fixture_scoped = {"odds_updates", "odds_snapshot", "scores_updates"}
    targets = probe_targets("https://x/api", fixture_id=None)
    results = [
        build_skipped_result(t)
        if t["name"] in fixture_scoped
        else {**t, "status": 200, "payload_note": "ok", "coverage_note": "ok"}
        for t in targets
    ]
    out = render_matrix(results)
    rows = {name: next(line for line in out.split("\n") if f"| {name} |" in line) for name in fixture_scoped}
    assert all("SKIPPED" in row for row in rows.values())
