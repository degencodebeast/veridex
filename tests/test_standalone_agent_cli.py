"""WD-3 — the veridex-agent CLI runs the standalone core end-to-end over a replay fixture."""

from __future__ import annotations

from veridex_agent.cli import build_arg_parser, main, run_from_config


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
