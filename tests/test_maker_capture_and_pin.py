"""E9-T3: the operator ``prepare``/``seal`` capture-and-pin CLI.

The CLI is **composition-only**: it orchestrates the already-built E3 capture, E2
config/pin, and E9 sealed-runner functions and adds NO decode/hash/pin/seal logic of
its own. It is deliberately SPLIT into two subcommands so scoring can never self-pin
its own config:

* ``prepare`` captures the ``OrderFilled`` artifact, writes it, and PRINTS + WRITES a
  pin-manifest (``trade_artifact_hash`` + ``config_hash``) -- the predeclaration the
  operator reviews and commits. ``prepare`` NEVER seals.
* ``seal`` rebuilds the config from the committed artifact and runs the sealed arena
  using the operator's PASSED ``--expected-config-hash`` (the committed predeclaration)
  -- never ``cfg.config_hash()`` recomputed live (that tautology could never VOID).

No test here touches the network: the capture is monkeypatched (or exercised on its
fail-closed path), so the CLI runs end-to-end OFFLINE.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import veridex.maker.runner as runner_mod
from veridex.maker.capture import build_trade_artifact
from veridex.maker.config import MakerVoidError, build_maker_run_config
from veridex.maker.mapping import DEFAULT_MAPPING_PATH, load_resolved_market_lookup
from veridex.maker.runner import CP1_18, RESULT_PATH
from veridex.maker.trade_artifact import (
    NormalizedTradeRow,
    load_trade_artifact,
    recompute_artifact_hash,
)
from veridex.maker.trades import AggressorSide

# A REAL pinned cp1 (condition_id, token_id) whose fixture is in the pinned cp1 set,
# so a decoded row using this token reconciles as ``rows_matched_cp1``.
_RECORDS, _ = load_resolved_market_lookup(DEFAULT_MAPPING_PATH)
_CP1_RECORD = next(r for r in _RECORDS if r.fixture_id in CP1_18)

# Descriptive manifest for build_trade_artifact (carries no operator secret); the
# count / hash / mapping-pin / rows fields are computed by the builder.
_MANIFEST_META = dict(
    raw_artifact_hash=None, schema_version="v1", decoder_version="d1", decoder_commit=None,
    source="polymarket_ctf_exchange_v2_orderfilled", chain_id=137, contract_address="0xe11...",
    event_signature="OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)",
    from_block=1, to_block=2, reorg_buffer_confs=20, capture_ts=1, capture_tool_id="t1",
    provider_id="hs-prod", token_supplied_externally=True, fixture_count=18, side_count=54,
    cleanroom_attestation="clean-room; no GPL copied",
)


def _row(**kw) -> NormalizedTradeRow:
    # A decoder-shaped row with a REAL cp1 token_id (condition_id="" as the ABI decode
    # leaves it; build_trade_artifact enriches it from the pinned mapping).
    base = dict(ts=1, price=0.5, size=2.0, aggressor_side=AggressorSide.BUY,
                condition_id="", token_id=_CP1_RECORD.token_id,
                block_number=100, tx_hash="0xabc", log_index=3)
    base.update(kw)
    return NormalizedTradeRow(**base)


def _write_fixture_artifact(out_path, rows):
    """Build a valid, cp1-matched ``TradeArtifact`` and write it as JSON (no network).

    Stands in for the real ``capture_order_filled_artifact`` in tests: it uses the same
    offline ``build_trade_artifact`` assembler + writes the artifact in the exact JSON
    shape ``load_trade_artifact`` reads back.
    """
    artifact = build_trade_artifact(
        list(rows),
        records=[{"token_id": _CP1_RECORD.token_id, "condition_id": _CP1_RECORD.condition_id}],
        manifest_meta=dict(_MANIFEST_META),
    )
    Path(out_path).write_text(
        json.dumps(artifact.model_dump(mode="json"), sort_keys=True, indent=2)
    )
    return artifact


def _prepared_artifact(tmp_path, rows):
    """Write a prepared artifact to ``tmp_path`` and return its path."""
    out = tmp_path / "cp1-trades.json"
    _write_fixture_artifact(out, rows)
    return out


def _fake_tape():
    # Two markets of CP1_18[0] so scoring produces markouts against each market's own fv
    # series -- reused from tests/test_maker_runner_r15.py.
    return [{"ts": 0, "fixture_id": CP1_18[0], "tick_seq": 0,
             "txline_market_key": "1X2_PARTICIPANT_RESULT||", "txline_side": "part1",
             "venue_market_ref": "1X2|home|full", "venue_side": "home",
             "fv": 0.60, "mid": 0.58, "staleness_s": 0},
            {"ts": 0, "fixture_id": CP1_18[0], "tick_seq": 0,
             "txline_market_key": "1X2_PARTICIPANT_RESULT||", "txline_side": "part2",
             "venue_market_ref": "1X2|away|full", "venue_side": "away",
             "fv": 0.30, "mid": 0.28, "staleness_s": 0}]


def run_cli(argv):
    """Invoke ``capture_and_pin.main`` in-process, capturing stdout/stderr/exit code.

    Catches ``SystemExit`` (argparse / fail-closed exits) and records its code; lets a
    ``MakerVoidError`` propagate so the anti-self-pin VOID surfaces to the caller.
    """
    from scripts.maker import capture_and_pin

    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            capture_and_pin.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return SimpleNamespace(stdout=out.getvalue(), stderr=err.getvalue(), exit_code=code)


# --- RED test 1: prepare writes + prints the predeclaration, and NEVER seals ---------
def test_prepare_writes_manifest_and_prints_hashes(tmp_path, monkeypatch):
    out = tmp_path / "cp1-trades.json"
    before = RESULT_PATH.read_text()
    monkeypatch.setattr(
        "scripts.maker.capture_and_pin.capture_order_filled_artifact",
        lambda **k: _write_fixture_artifact(k["out_path"], rows=[_row()]),
    )
    res = run_cli(["prepare", "--from-block", "1", "--to-block", "2", "--out", str(out)])
    assert res.exit_code == 0

    art = load_trade_artifact(out)
    h = recompute_artifact_hash(list(art.rows))
    cfg_h = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=art).config_hash()

    manifest = json.loads(out.with_suffix(".pin.json").read_text())
    assert manifest["trade_artifact_hash"] == h
    assert manifest["config_hash"] == cfg_h
    assert manifest["out_path"] == str(out)
    assert manifest["from_block"] == 1 and manifest["to_block"] == 2
    assert manifest["rows_matched_cp1"] == art.rows_matched_cp1

    # BOTH predeclared hashes are printed for the operator to review + commit.
    assert h in res.stdout and cfg_h in res.stdout
    # prepare NEVER seals: the committed sealed result is untouched.
    assert RESULT_PATH.read_text() == before


# --- RED test 2: the ANTI-SELF-PIN proof -- a WRONG predeclared hash must VOID --------
def test_seal_rejects_config_hash_mismatch(tmp_path):
    out = _prepared_artifact(tmp_path, rows=[_row()])
    before = RESULT_PATH.read_text()
    with pytest.raises((MakerVoidError, SystemExit)):
        run_cli(["seal", "--artifact", str(out),
                 "--expected-config-hash", "WRONG_PREDECLARED_HASH"])
    # Drift from the predeclaration -> VOID before any sealed write.
    assert RESULT_PATH.read_text() == before


# --- RED test 3: seal with the CORRECT predeclared hash -> sealed MM-R1.5 -------------
def test_seal_with_predeclared_hash_produces_r15(tmp_path, monkeypatch):
    out = _prepared_artifact(tmp_path, rows=[_row()])
    art = load_trade_artifact(out)
    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=art)
    predeclared = cfg.config_hash()  # the hash the operator would commit from prepare's manifest

    sealed_path = tmp_path / "sealed-result.json"
    monkeypatch.setattr(runner_mod, "RESULT_PATH", sealed_path)  # do NOT clobber the committed result
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())

    res = run_cli(["seal", "--artifact", str(out), "--expected-config-hash", predeclared])
    assert res.exit_code == 0

    # sealed MM-R1.5, artifact-hash recorded, executable edge the literal null.
    assert "R1_5" in res.stdout
    assert recompute_artifact_hash(list(art.rows)) in res.stdout
    assert "real_executable_edge_bps: None" in res.stdout
    assert sealed_path.exists()
    sealed = json.loads(sealed_path.read_text())
    assert sealed["real_executable_edge_bps"] is None


# --- RED test 4: prepare fails CLOSED with no token + no injected client --------------
def test_prepare_fails_closed_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv("HYPERSYNC_API", raising=False)
    out = tmp_path / "cp1-trades.json"
    # capture is NOT monkeypatched -> the real fail-closed guard runs (no network).
    res = run_cli(["prepare", "--from-block", "1", "--to-block", "2", "--out", str(out)])
    assert res.exit_code != 0
    assert not out.exists()                       # no partial artifact written
    assert not out.with_suffix(".pin.json").exists()  # no partial pin-manifest written


# --- RED test 5: the CLI NEVER emits the operator token anywhere ----------------------
def test_cli_never_emits_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERSYNC_API", "SECRET123")
    out = tmp_path / "cp1-trades.json"
    monkeypatch.setattr(
        "scripts.maker.capture_and_pin.capture_order_filled_artifact",
        lambda **k: _write_fixture_artifact(k["out_path"], rows=[_row()]),
    )
    res = run_cli(["prepare", "--from-block", "1", "--to-block", "2", "--out", str(out)])
    assert res.exit_code == 0

    assert "SECRET123" not in res.stdout
    assert "SECRET123" not in res.stderr
    assert "SECRET123" not in out.read_text()
    assert "SECRET123" not in out.with_suffix(".pin.json").read_text()


# --- RED test 6: the LIVE capture path never leaks the token on ANY exception ---------
@pytest.mark.parametrize("exc_type", [RuntimeError, ValueError])
def test_cli_never_leaks_token_in_capture_error(tmp_path, monkeypatch, exc_type):
    """A network-SDK error on the operator LIVE path must fail closed, token-free.

    ``prepare`` previously only caught ``RuntimeError`` (the fail-closed guard). On the
    operator LIVE path (token present + real adapter), a network-SDK exception is
    typically NOT a ``RuntimeError`` and would propagate as a raw traceback -- and such
    an SDK error can embed the token (e.g. in a request URL). This proves BOTH: (1) a
    RuntimeError whose message happens to contain the token is still scrubbed, and (2) a
    non-RuntimeError exception is now caught at all (the broadened catch).
    """
    monkeypatch.setenv("HYPERSYNC_API", "SECRET_TOKEN_XYZ")
    out = tmp_path / "cp1-trades.json"

    def _raise(**_kw):
        raise exc_type(
            "GET https://hypersync/query?api_key=SECRET_TOKEN_XYZ failed 500"
        )

    monkeypatch.setattr("scripts.maker.capture_and_pin.capture_order_filled_artifact", _raise)
    res = run_cli(["prepare", "--from-block", "1", "--to-block", "2", "--out", str(out)])

    assert res.exit_code != 0
    assert "SECRET_TOKEN_XYZ" not in res.stdout
    assert "SECRET_TOKEN_XYZ" not in res.stderr
    assert not out.exists()
    assert not out.with_suffix(".pin.json").exists()
