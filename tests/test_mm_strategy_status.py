"""Read-only Gamma market-status authority guards (MM-R4-B — REQ-027 / AC-053).

Offline by construction: every test injects a FAKE fetch seam returning canned Gamma
market metadata, so nothing here makes a live/credentialed Gamma call. Three invariants are
pinned:

1. the total, disjoint ``active``/``closed`` → :data:`MarketStatus` map (``closed`` WINS);
2. the fail-closed contract — any fetch/parse failure or missing/malformed flag maps to
   ``UNKNOWN`` with the ``(None, None)`` sentinels (never a fabricated ``ACTIVE``);
3. a static/AST scan proving the module imports NOTHING that submits/cancels/signs.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any

import veridex.venues.market_status as market_status
from veridex.venues.market_status import GammaMarketStatusAuthority


def _authority(
    metadata_by_ref: dict[str, Any],
    *,
    epoch: int = 7,
    recv_ts: int = 1000,
) -> GammaMarketStatusAuthority:
    """Build an authority whose fetch seam is a pure lookup over canned metadata (offline).

    An unknown ``venue_market_ref`` raises ``KeyError`` inside the fetch — exercising the
    fetch-failure → UNKNOWN path without any network.
    """

    def fetch(venue_market_ref: str) -> Any:
        return metadata_by_ref[venue_market_ref]

    return GammaMarketStatusAuthority(
        fetch=fetch, clock=lambda: recv_ts, epoch=epoch
    )


def test_gamma_active_closed_maps_total_and_disjoint() -> None:
    # All four boolean cells of (active, closed) resolve to exactly one deterministic status,
    # and a CLOSED market wins even when it is also flagged active (REQ-027; closed WINS).
    cases: dict[tuple[bool, bool], str] = {
        (True, False): "ACTIVE",
        (True, True): "CLOSED",  # closed wins over active
        (False, True): "CLOSED",  # closed wins (¬active does not matter once closed)
        (False, False): "HALTED",
    }
    for (active, closed), expected in cases.items():
        metadata = {"active": active, "closed": closed, "conditionId": "0xabc"}
        authority = _authority({"ref": metadata}, epoch=7, recv_ts=1000)

        status, recv_ts, epoch = authority.read("ref")

        assert status == expected, f"(active={active}, closed={closed}) → {status!r}"
        # A definite (non-UNKNOWN) status carries non-None sentinels: recv_ts is the read
        # clock, epoch the authority generation.
        assert recv_ts == 1000
        assert epoch == 7


def test_read_failure_is_unknown_with_none_sentinels() -> None:
    # Every failure/missing/malformed shape maps to (UNKNOWN, None, None) — the fail-closed
    # honesty rule: the authority never fabricates a definite status it cannot prove.
    # (a) fetch failure (ref absent from the canned map → KeyError inside the seam).
    authority = _authority({}, epoch=7, recv_ts=1000)
    assert authority.read("missing") == ("UNKNOWN", None, None)

    # (b) missing active/closed flags.
    only_active = _authority({"ref": {"active": True}}, epoch=7)
    assert only_active.read("ref") == ("UNKNOWN", None, None)
    only_closed = _authority({"ref": {"closed": False}}, epoch=7)
    assert only_closed.read("ref") == ("UNKNOWN", None, None)

    # (c) non-boolean flags (Gamma booleans are real JSON bools; a string/int is malformed).
    string_flags = _authority(
        {"ref": {"active": "true", "closed": "false"}}, epoch=7
    )
    assert string_flags.read("ref") == ("UNKNOWN", None, None)

    # (d) a non-mapping fetch result (e.g. an empty list) is malformed, not a market object.
    non_mapping = _authority({"ref": []}, epoch=7)
    assert non_mapping.read("ref") == ("UNKNOWN", None, None)


def test_module_imports_no_write_or_signer() -> None:
    # Static/AST proof the read-only authority pulls in NOTHING that could submit, cancel, or
    # sign: not veridex.venues.base / veridex.venues.sx_bet, and no submit_order/cancel_order
    # /private-key/EIP-712 symbol. (The mutation adds a sx_bet import → this test fails.)
    source = inspect.getsource(market_status)
    tree = ast.parse(source)

    imported_paths: set[str] = set()
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_paths.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_paths.add(node.module)
            for alias in node.names:
                identifiers.add(alias.name)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)

    forbidden_paths = {"veridex.venues.base", "veridex.venues.sx_bet"}
    offending_imports = {
        path
        for path in imported_paths
        for bad in forbidden_paths
        if path == bad or path.startswith(bad + ".")
    }
    assert not offending_imports, f"read-only module imports a write/venue module: {offending_imports}"

    forbidden_symbols = {"submit_order", "cancel_order"}
    offending_symbols = identifiers & forbidden_symbols
    assert not offending_symbols, f"read-only module references a write symbol: {offending_symbols}"

    # Signer markers are checked against CODE symbols (imported paths + identifiers) ONLY — never
    # raw prose — so a docstring naming what the module refuses to import can't trip the bar (the
    # same code-only discipline as the pure-tier import audit).
    code_symbols = " ".join(imported_paths | identifiers).lower()
    for marker in ("private_key", "eip712", "eip_712"):
        assert marker not in code_symbols, f"read-only module references a signer marker: {marker!r}"
