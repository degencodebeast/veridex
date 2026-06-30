"""WD-3 — the ``veridex-agent`` CLI: a thin wrapper over the standalone-run core (REQ-052).

``veridex-agent run --config agent.toml [--fixture PATH]`` loads a non-secret run config, builds
the agent + market data (replay fixture or live stream), runs the decoupled standalone core, and
prints the verified-proof summary (run id, CLV, manifest hash, anchor status, verify verdict).
Credentials come from ``veridex.config.Settings`` (env / ``veridex/.env``), never from the CLI.
"""

from __future__ import annotations

import argparse
import asyncio

from veridex.chain.anchor import anchor_memo
from veridex.ingest.marketstate import replay_marketstates
from veridex_agent.config import build_agent, load_agent_run_config
from veridex_agent.run import StandaloneRunResult, standalone_run


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the ``veridex-agent`` argument parser.

    Returns:
        A parser exposing the ``run`` subcommand with ``--config`` and optional ``--fixture``.
    """
    parser = argparse.ArgumentParser(prog="veridex-agent", description="Run one standalone Veridex agent.")
    sub = parser.add_subparsers(dest="command", required=True)
    run_cmd = sub.add_parser("run", help="Run one agent under law/policy/proof and emit a proof.")
    run_cmd.add_argument("--config", required=True, help="Path to a non-secret run-config TOML.")
    run_cmd.add_argument("--fixture", default=None, help="Override the replay fixture path.")
    return parser


async def run_from_config(config_path: str, *, fixture_override: str | None = None) -> StandaloneRunResult:
    """Load a run config and execute the standalone-run core.

    Args:
        config_path: Path to the run-config TOML.
        fixture_override: Optional replay fixture path overriding the config value.

    Returns:
        The :class:`~veridex_agent.run.StandaloneRunResult`.

    Raises:
        ValueError: If a replay run has no fixture path, or a live run is requested (the live
            stream path is wired via ``veridex.ingest.live_client`` and needs configured creds —
            documented in the deploy README).
    """
    config = load_agent_run_config(config_path)
    agent = build_agent(config)

    if config.source_mode == "replay":
        fixture_path = fixture_override or config.fixture_path
        if not fixture_path:
            raise ValueError("replay run requires a fixture_path (in the TOML or via --fixture)")
        marketstates = replay_marketstates(fixture_path)
    else:
        raise ValueError("live source_mode requires configured TxLINE creds; see docs/deploy-your-own-agent.md")

    anchor_fn = anchor_memo if config.anchor else None
    return await standalone_run(marketstates, agent, source_mode=config.source_mode, anchor_fn=anchor_fn)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (``0`` on a verified run, ``1`` on failure).
    """
    args = build_arg_parser().parse_args(argv)
    if args.command == "run":
        result = asyncio.run(run_from_config(args.config, fixture_override=args.fixture))
        avg_clv = result.scores[0]["avg_clv_bps"] if result.scores else None
        verdict = "VERIFIED" if result.verified else "VERIFY-FAILED"
        print(
            f"[{verdict}] run_id={result.run_id} source={result.source_mode} "
            f"avg_clv_bps={avg_clv} manifest_hash={result.manifest_hash} anchor={result.anchor_status}"
        )
        return 0 if result.verified else 1
    return 2
