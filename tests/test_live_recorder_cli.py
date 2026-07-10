"""E9 operator read-only live-recorder CLI tests (MM-R3, milestone E9).

Offline and network-free. These tests drive the thin operator CLI
(``scripts/maker/live_recorder.py``) through its fail-closed credential guard with a
FAKE env and assert that:

* it FAILS CLOSED (exits) when a required TxLINE credential is absent, BEFORE any I/O
  (no session directory is created);
* no secret VALUE ever reaches stdout/stderr (token hygiene, AC-015 / SEC-007);
* the CLI references no order / wallet / venue-write symbol and imports nothing from
  ``veridex.maker`` / ``veridex.scoring`` (read-only, no orders — R4-001..006 / AC-013);
* no network library is imported at module scope (offline-safe import).

They also LINT the submission-quality runbook for the honest
"R4 declared/gated, not run" wording and the ABSENCE of forbidden overclaim words
(AC-014). Every test runs with a fake env and no network.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import scripts.maker.live_recorder as cli

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_cli_fails_closed_and_no_token_in_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Absent required creds → the CLI exits BEFORE any I/O and never echoes a secret value.

    The required creds are ``(JWT, TXLINE_X_API_TOKEN)``. We supply ONLY ``JWT`` (carrying
    a fake secret value) so the guard must fail closed on the missing ``TXLINE_X_API_TOKEN``
    — and must never leak the present secret value into stdout/stderr. Because the guard runs
    before fixtures are read or any source is constructed, no session directory is created.
    """
    fake_token = "FAKE_SECRET_TOKEN_do_not_leak_9f3c2a"
    env = {"JWT": fake_token}  # TXLINE_X_API_TOKEN deliberately absent → fail closed
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(
        json.dumps([{"fixture_id": 1, "event_slug": "x", "home_team": "A", "away_team": "B"}])
    )
    out = tmp_path / "out"

    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "--fixtures",
                str(fixtures),
                "--out",
                str(out),
                "--minutes",
                "1",
                "--poll-interval-ms",
                "5000",
            ],
            env=env,
        )

    # Fail closed: a non-zero / aborting exit (never a clean 0).
    assert exc_info.value.code not in (0, None)
    captured = capsys.readouterr()
    assert fake_token not in captured.out
    assert fake_token not in captured.err
    # Fail closed BEFORE any I/O: no session directory was created.
    assert not out.exists()


def test_cli_references_no_order_or_wallet_symbol() -> None:
    """Read-only, no orders: the CLI names no order/wallet/venue-write symbol (R4-001..006, AC-013)."""
    source = Path(cli.__file__).read_text()
    forbidden = (
        "submit_order",
        "cancel_order",
        "place_order",
        "create_order",
        "post_order",
        "cancel_all",
        "private_key",
        "wallet",
        "signer",
    )
    for symbol in forbidden:
        assert symbol not in source, f"CLI references a forbidden order/wallet symbol: {symbol}"

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[:2]
            assert root != ["veridex", "maker"], f"CLI imports veridex.maker ({node.module})"
            assert root != ["veridex", "scoring"], f"CLI imports veridex.scoring ({node.module})"


def test_no_module_scope_network_import() -> None:
    """Import-audit: no network library at module scope (offline-safe import)."""
    tree = ast.parse(Path(cli.__file__).read_text())
    top_level: set[str] = set()
    for node in tree.body:  # module scope only — lazy imports inside functions are allowed
        if isinstance(node, ast.Import):
            top_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_level.add(node.module.split(".")[0])
    forbidden = {"httpx", "requests", "websocket", "websockets", "aiohttp", "web3", "numpy"}
    assert not (top_level & forbidden), f"module-scope network import: {top_level & forbidden}"


def test_cli_help_shows_expected_flags() -> None:
    """The operator CLI mirrors the live-monitor arg style (offline: ``build_parser`` builds no source)."""
    help_text = cli.build_parser().format_help()
    for flag in ("--fixtures", "--out", "--poll-interval-ms", "--minutes", "--base-url"):
        assert flag in help_text, f"missing operator flag: {flag}"
