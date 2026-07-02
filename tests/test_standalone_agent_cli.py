"""WD-3 — the veridex-agent CLI runs the standalone core end-to-end over a replay fixture."""

from __future__ import annotations

from pathlib import Path

from tests.test_standalone_run import _live_close, _live_stream, _live_ticks
from veridex_agent.cli import build_arg_parser, main, run_from_config
from veridex_agent.run import StandaloneRunResult


def test_arg_parser_has_run_subcommand() -> None:
    parser = build_arg_parser()
    ns = parser.parse_args(["run", "--config", "x.toml"])
    assert ns.command == "run"
    assert ns.config == "x.toml"


async def test_run_from_config_replay() -> None:
    # The sample TOML uses the WD-2 replay fixture + momentum strategy, anchor disabled.
    result = await run_from_config("veridex_agent/sample_agent.toml")
    assert result.verified is True
    assert result.anchor_status == "not_anchored"


def test_main_run_exits_zero(capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["run", "--config", "veridex_agent/sample_agent.toml"])
    assert code == 0
    out = capsys.readouterr().out
    assert "VERIFIED" in out


def test_main_run_missing_config_is_clean_error(capsys) -> None:  # type: ignore[no-untyped-def]
    # A bad/missing config must produce a clean operator-facing error + nonzero exit, NOT a traceback.
    code = main(["run", "--config", "does-not-exist.toml"])
    assert code == 1
    err = capsys.readouterr().err
    assert "veridex-agent: error:" in err


_LIVE_TOML = """
agent_id = "live-desk-1"
strategy = "baseline"
source_mode = "live"
window_id = "w1"
fixture_id = 1
end_rule = "pre_match"
market_allowlist = ["OU"]
execution_mode = "paper"
anchor = false
"""


async def test_run_from_config_live_no_longer_raises(tmp_path: Path) -> None:
    # T20: source_mode="live" NO LONGER raises. With an injected stream (no network), the CLI
    # builds a RunWindow from the TOML and drives the live launch path to a verified run result.
    config_path = tmp_path / "live_agent.toml"
    config_path.write_text(_LIVE_TOML)

    result = await run_from_config(
        str(config_path),
        stream=_live_stream(_live_ticks()),
        fetch_updates=_live_close,
    )
    assert isinstance(result, StandaloneRunResult)
    assert result.source_mode == "live"
    assert result.verified is True
