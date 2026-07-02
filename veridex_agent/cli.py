"""WD-3 / Phase-2D T20 — the ``veridex-agent`` CLI: a thin wrapper over the standalone core (REQ-052).

``veridex-agent run --config agent.toml [--fixture PATH]`` loads a non-secret run config, builds the
agent + market source (replay fixture or a live TxLINE window), runs the decoupled standalone core,
and prints the verified-proof summary (run id, CLV, manifest hash, anchor status, verify verdict).
Credentials come from ``veridex.config.Settings`` (env / ``veridex/.env``), never from the CLI.

T20: ``source_mode = "live"`` NO LONGER raises — it builds a :class:`~veridex.runtime.window.RunWindow`
from the TOML and drives the live launch path (the live TxLINE client + creds live inside the runner
seam). When ``execution_mode != "paper"`` the standalone core also runs the policy-gated execution
lane (NON-SCORING receipts). Tests inject ``stream`` + ``fetch_updates`` for a fully offline live run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from veridex.chain.anchor import anchor_memo
from veridex.ingest.marketstate import MarketState, replay_marketstates
from veridex_agent.config import build_agent, build_policy_envelope, build_run_window, load_agent_run_config
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


async def run_from_config(
    config_path: str,
    *,
    fixture_override: str | None = None,
    stream: AsyncIterator[MarketState] | None = None,
    fetch_updates: Callable[[int], Awaitable[list[dict[str, Any]]]] | None = None,
    adapter: Any | None = None,
) -> StandaloneRunResult:
    """Load a run config and execute the standalone-run core (replay or live).

    Args:
        config_path: Path to the run-config TOML.
        fixture_override: Optional replay fixture path overriding the config value.
        stream: Injected live tick stream (tests / offline live runs); ``None`` → the real TxLINE
            stream inside the live runner seam.
        fetch_updates: Injected CON-040 close fetch (tests); ``None`` → the real fetch inside the seam.
        adapter: Injected venue adapter for the execution lane; ``None`` → picked by execution_mode.

    Returns:
        The :class:`~veridex_agent.run.StandaloneRunResult`.

    Raises:
        ValueError: If a replay run has no fixture path.
    """
    config = load_agent_run_config(config_path)
    agent = build_agent(config)
    anchor_fn = anchor_memo if config.anchor else None
    # The execution lane only engages for a non-paper mode; paper stays proof-only (no envelope).
    policy_envelope = build_policy_envelope(config) if config.execution_mode != "paper" else None
    config_hash = config.config_hash()

    if config.source_mode == "replay":
        fixture_path = fixture_override or config.fixture_path
        if not fixture_path:
            raise ValueError("replay run requires a fixture_path (in the TOML or via --fixture)")
        marketstates = replay_marketstates(fixture_path)
        return await standalone_run(
            marketstates,
            agent,
            source_mode="replay",
            policy_envelope=policy_envelope,
            execution_mode=config.execution_mode,
            adapter=adapter,
            config_hash=config_hash,
            anchor_fn=anchor_fn,
        )

    # Live launch path (T20): build the window from the TOML and drive the live runner.
    return await standalone_run(
        [],
        agent,
        window=build_run_window(config),
        stream=stream,
        fetch_updates=fetch_updates,
        policy_envelope=policy_envelope,
        execution_mode=config.execution_mode,
        adapter=adapter,
        config_hash=config_hash,
        anchor_fn=anchor_fn,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (``0`` on a verified run, ``1`` on failure).
    """
    args = build_arg_parser().parse_args(argv)
    if args.command == "run":
        try:
            result = asyncio.run(run_from_config(args.config, fixture_override=args.fixture))
        except (ValueError, FileNotFoundError) as exc:
            # Clean operator-facing error (bad/missing fixture, live mode without creds) — no traceback.
            print(f"veridex-agent: error: {exc}", file=sys.stderr)
            return 1
        avg_clv = result.scores[0]["avg_clv_bps"] if result.scores else None
        verdict = "VERIFIED" if result.verified else "VERIFY-FAILED"
        print(
            f"[{verdict}] run_id={result.run_id} source={result.source_mode} "
            f"avg_clv_bps={avg_clv} manifest_hash={result.manifest_hash} anchor={result.anchor_status}"
        )
        return 0 if result.verified else 1
    return 2
