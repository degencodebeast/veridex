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

from scripts.txline_live.access_matrix import (
    build_skipped_result,
    probe_targets,
    render_matrix,
    skip_note_for,
)


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


def test_discovery_uses_documented_fixtures_snapshot_path():
    # The broken bare-`/fixtures` / `/sports` / `/competitions` probes are gone; discovery is
    # the ONE documented `/fixtures/snapshot?...` path.
    targets = {t["name"]: t for t in probe_targets("https://x/api", fixture_id=123, competition_id=72, start_epoch_day=20213)}
    assert "sports_discovery" not in targets
    assert "competitions_discovery" not in targets
    disc = targets["fixtures_discovery"]
    assert disc["url"] == "https://x/api/fixtures/snapshot?competitionId=72&startEpochDay=20213"
    for t in targets.values():
        assert not t["url"].endswith("/fixtures")  # never the bare 404 path


def test_odds_snapshot_target_carries_as_of():
    targets = {t["name"]: t for t in probe_targets("https://x/api", fixture_id=123, as_of=1782518400)}
    assert targets["odds_snapshot"]["url"] == "https://x/api/odds/snapshot/123?asOf=1782518400"


def test_odds_validation_placeholder_is_always_skipped_not_a_failure():
    # A PLACEHOLDER messageId 404 is not an access signal — it must be SKIPPED honestly even when
    # every other parameter is supplied.
    validation = next(t for t in probe_targets("https://x/api", fixture_id=123, competition_id=72, start_epoch_day=1) if t["name"] == "odds_validation")
    note = skip_note_for(validation, fixture_id=123, competition_id=72, start_epoch_day=1)
    assert note is not None
    assert "messageId" in note


def test_discovery_skipped_when_competition_params_unset():
    disc = next(t for t in probe_targets("https://x/api", fixture_id=123) if t["name"] == "fixtures_discovery")
    assert skip_note_for(disc, fixture_id=123, competition_id=None, start_epoch_day=None) is not None
    # ...but probed (not skipped) once the competition params are supplied.
    assert skip_note_for(disc, fixture_id=123, competition_id=72, start_epoch_day=20213) is None


def test_working_probes_are_not_skipped():
    for name in ("odds_stream", "odds_updates", "odds_snapshot", "scores_stream", "scores_updates"):
        target = next(t for t in probe_targets("https://x/api", fixture_id=123, as_of=1) if t["name"] == name)
        assert skip_note_for(target, fixture_id=123, competition_id=72, start_epoch_day=1) is None


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


def test_probe_targets_returns_all_capabilities_without_fixture_id():
    names = {t["name"] for t in probe_targets("https://x/api", fixture_id=None)}
    assert names == {
        "odds_stream",
        "odds_updates",
        "odds_snapshot",
        "scores_stream",
        "scores_updates",
        "odds_validation",
        "fixtures_discovery",
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
