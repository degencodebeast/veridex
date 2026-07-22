"""Operator CLI entrypoint (D4) — seed the Official Replay League against the REAL served app.

This is the ONE command an operator runs to seed the two official directional agents end-to-end:

    APP_ENV=production python -m scripts.seed_official_replay_league --seed-revision <rev>

It builds the SAME app shape ``veridex.api.server`` serves (via the reusable ``create_server_app``
builder — durable Postgres when ``DATABASE_URL`` is set, else explicit InMemory local-dev) and drives
the D3 :func:`~veridex.seed.official_replay_league.run_seed` state machine over that app. Nothing runs
at import time and importing this module has NO side effects (no app is built, no seed runs): ALL real
work happens inside functions reached only from :func:`main`, and the ``__main__`` guard is the ONLY
auto-run path. This keeps the module importable in the offline test env (no ``DATABASE_URL`` /
``CORS_ORIGINS`` required just to import it) and lets tests monkeypatch the seams.

Design for testability:
  * ``build_app_and_store`` is a module-level seam tests monkeypatch to skip building the real app/PG.
  * ``run_seed`` is referenced at MODULE level so tests can monkeypatch ``<thismodule>.run_seed``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import TYPE_CHECKING

from veridex.api.server import create_server_app
from veridex.seed.official_replay_league import run_seed

if TYPE_CHECKING:
    from fastapi import FastAPI

    from veridex.store import Store

#: Defaults for the two timing knobs (kept in sync with the D3 ``run_seed`` signature). The
#: wait-timeout ceiling is intentionally raisable on the CLI: F1 runs the seal over contended
#: container Postgres, so the operator must be able to lift it past the default.
_DEFAULT_WAIT_TIMEOUT_S = 30.0
_DEFAULT_POLL_INTERVAL_S = 0.05


def build_app_and_store() -> tuple[FastAPI, Store]:
    """Build the REAL served app + its store, mirroring ``veridex.api.server`` exactly.

    Delegates to the reusable :func:`~veridex.api.server.create_server_app` builder (rather than
    reinventing a divergent app) so the seed drives the identical composition the container serves:
    durable ``PostgresStore`` over an ``AsyncConnectionPool`` when ``DATABASE_URL`` is set (opened +
    reachability-checked + ``init_db`` run via the app's Postgres lifespan, which :func:`_run` enters),
    else an explicit InMemory local-dev store. Returns the composed inner FastAPI (``guard.app``) — the
    app carrying the real deploy/competition routes + ``app.state.store`` — so ``run_seed`` seeds the
    SAME durable rows a running F1 container observes over shared Postgres.

    Returns:
        ``(app, store)``: the composed FastAPI and the SAME store it was built with.
    """
    guard = create_server_app(env=os.environ)
    app = guard.app
    store: Store = app.state.store
    return app, store


async def _run(args: argparse.Namespace) -> None:
    """Build the real app+store and drive :func:`run_seed`, printing the result for operator visibility.

    The app's lifespan is entered around the seed when present, so on the Postgres path the pool is
    OPEN (and ``init_db`` has run) for the duration of the seed — both the in-process routes AND the
    durable rows are live. When ``build_app_and_store`` yields a bare sentinel (tests) or an app with
    no lifespan, the seed runs directly.
    """
    app, store = build_app_and_store()
    lifespan = getattr(getattr(app, "router", None), "lifespan_context", None)
    if lifespan is None:
        result = await run_seed(
            app,
            store,
            seed_revision=args.seed_revision,
            operator_token=args.operator_token,
            wait_timeout_s=args.wait_timeout_s,
            poll_interval_s=args.poll_interval_s,
        )
    else:
        async with lifespan(app):
            result = await run_seed(
                app,
                store,
                seed_revision=args.seed_revision,
                operator_token=args.operator_token,
                wait_timeout_s=args.wait_timeout_s,
                poll_interval_s=args.poll_interval_s,
            )

    print(
        "seed complete: "
        f"revision={result.seed_revision} "
        f"public_agents={len(result.public_agent_ids)} "
        f"instances={len(result.instance_ids)} "
        f"competitions={len(result.competition_ids)} "
        f"runs={len(result.run_ids)} "
        f"projected_rows={result.projected_row_count}"
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the operator CLI arguments (thin argparse; no side effects beyond parsing)."""
    parser = argparse.ArgumentParser(
        prog="seed_official_replay_league",
        description="Seed the Official Replay League against the real served app.",
    )
    parser.add_argument(
        "--seed-revision",
        required=True,
        help="Stable seed-run identity the durable ledger is keyed by (idempotency key).",
    )
    parser.add_argument(
        "--wait-timeout-s",
        type=float,
        default=_DEFAULT_WAIT_TIMEOUT_S,
        help="Max seconds to await BOTH deployments reaching SEALED (fail closed on timeout).",
    )
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=_DEFAULT_POLL_INTERVAL_S,
        help="Delay between deploy-status polls.",
    )
    parser.add_argument(
        "--operator-token",
        default=None,
        help="Bearer token sent on every seed request; omit to rely on AUTH_MODE=dev.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Parse args and run the seed. The ONLY entrypoint that performs work (import stays side-effect-free)."""
    args = _parse_args(argv)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
