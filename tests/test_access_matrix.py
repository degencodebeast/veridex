"""T1 — TxLINE access-matrix probe tests (REQ-2D-001 / AC-2D-001).

TDD Iron Law: written RED (ImportError — module missing) before the
production module existed.

Behaviors under test
---------------------
- ``probe_targets``: PURE target builder covering every spec capability
  (odds stream/updates/snapshot, scores stream/updates, odds validation,
  fixtures discovery), regardless of whether ``fixture_id`` is known.
- ``render_matrix``: PURE §4.6 markdown table renderer; unknown/unprobed
  cells render the literal ``UNKNOWN`` token (or an explicit ``SKIPPED``
  status), never silently blank.

Both functions are offline-only — no network, no real creds. ``main()`` is
the only live-touching code in ``access_matrix.py`` and is not exercised
here.
"""

from __future__ import annotations

from scripts.txline_live.access_matrix import probe_targets, render_matrix


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
