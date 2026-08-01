"""D4 — the OPERATOR CLI entrypoint that drives ``run_seed`` (TDD).

``scripts/seed_official_replay_league.py`` is the thin argparse CLI the operator runs to seed the
Official Replay League. It builds the REAL app+store (mirroring ``server.py``) and calls the D3
``run_seed``. These tests pin the CLI's argument wiring and its NO-IMPORT-SIDE-EFFECTS guarantee
WITHOUT building a real app or touching Postgres — ``build_app_and_store`` and ``run_seed`` are both
monkeypatched at the module level, so every test is offline and zero-wire.

Load-bearing properties (one test each):

1. ``main`` awaits ``run_seed`` EXACTLY once, forwarding the parsed ``--seed-revision`` and the app +
   store returned by ``build_app_and_store`` (the sentinels — no real app/PG is built).
2. The optional knobs default correctly (``wait_timeout_s==30.0``, ``poll_interval_s==0.05``,
   ``operator_token is None``) and CLI overrides are forwarded with the right types (floats / str).
3. Importing the module has NO side effects: it does NOT call ``run_seed`` or build the app, and it
   imports cleanly in the offline env WITHOUT ``DATABASE_URL`` / any serving env configured.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

import scripts.seed_official_replay_league as cli
from veridex.seed.official_replay_league import SeedError, SeedResult


def _empty_result(seed_revision: str) -> SeedResult:
    """A minimal printable :class:`SeedResult` the recording fake returns so ``main`` can print it."""
    return SeedResult(
        seed_revision=seed_revision,
        public_agent_ids=[],
        instance_ids=[],
        competition_ids=[],
        run_ids=[],
        projected_row_count=0,
    )


def _install_recording_run_seed(
    monkeypatch: Any, calls: list[dict[str, Any]]
) -> None:
    """Monkeypatch the module's ``run_seed`` with an async fake that records each awaited call."""

    async def fake_run_seed(
        app: Any,
        store: Any,
        *,
        seed_revision: str,
        operator_token: str | None,
        wait_timeout_s: float,
        poll_interval_s: float,
    ) -> SeedResult:
        calls.append(
            {
                "app": app,
                "store": store,
                "seed_revision": seed_revision,
                "operator_token": operator_token,
                "wait_timeout_s": wait_timeout_s,
                "poll_interval_s": poll_interval_s,
            }
        )
        return _empty_result(seed_revision)

    monkeypatch.setattr(cli, "run_seed", fake_run_seed)


def test_main_calls_run_seed_once_with_revision(monkeypatch: Any) -> None:
    sentinel_app = object()
    sentinel_store = object()
    calls: list[dict[str, Any]] = []
    _install_recording_run_seed(monkeypatch, calls)
    monkeypatch.setattr(
        cli, "build_app_and_store", lambda: (sentinel_app, sentinel_store)
    )

    cli.main(["--seed-revision", "r1"])

    assert len(calls) == 1
    assert calls[0]["seed_revision"] == "r1"
    assert calls[0]["app"] is sentinel_app
    assert calls[0]["store"] is sentinel_store


def test_defaults_and_overrides_forwarded(monkeypatch: Any) -> None:
    sentinel_app = object()
    sentinel_store = object()
    monkeypatch.setattr(
        cli, "build_app_and_store", lambda: (sentinel_app, sentinel_store)
    )

    # Defaults.
    calls: list[dict[str, Any]] = []
    _install_recording_run_seed(monkeypatch, calls)
    cli.main(["--seed-revision", "r1"])
    assert len(calls) == 1
    assert calls[0]["wait_timeout_s"] == 30.0
    assert isinstance(calls[0]["wait_timeout_s"], float)
    assert calls[0]["poll_interval_s"] == 0.05
    assert isinstance(calls[0]["poll_interval_s"], float)
    assert calls[0]["operator_token"] is None

    # Overrides.
    calls_over: list[dict[str, Any]] = []
    _install_recording_run_seed(monkeypatch, calls_over)
    cli.main(
        [
            "--seed-revision",
            "r2",
            "--wait-timeout-s",
            "90",
            "--poll-interval-s",
            "0.1",
            "--operator-token",
            "tok",
        ]
    )
    assert len(calls_over) == 1
    assert calls_over[0]["seed_revision"] == "r2"
    assert calls_over[0]["wait_timeout_s"] == 90.0
    assert isinstance(calls_over[0]["wait_timeout_s"], float)
    assert calls_over[0]["poll_interval_s"] == 0.1
    assert isinstance(calls_over[0]["poll_interval_s"], float)
    assert calls_over[0]["operator_token"] == "tok"


def test_import_has_no_side_effects(monkeypatch: Any) -> None:
    # Importing the CLI must not require a serving env: no DATABASE_URL / CORS_ORIGINS, no seed run.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)

    seed_called: list[bool] = []
    app_built: list[bool] = []

    async def _run_seed_tripwire(*_args: Any, **_kwargs: Any) -> SeedResult:
        seed_called.append(True)
        return _empty_result("never")

    def _build_app_tripwire(*_args: Any, **_kwargs: Any) -> Any:
        app_built.append(True)
        raise AssertionError("create_server_app must not run at import time")

    # `importlib.reload(cli)` RE-EXECUTES `from ... import run_seed` / `from ... import
    # create_server_app`, which rebinds cli.run_seed / would rebind a patched cli-level name back
    # to the real function -- patching cli.run_seed here would be silently discarded by the reload
    # and the assertion below would pass vacuously even if import DID invoke the seed/app-builder.
    # Patch the SOURCE attributes the `from` imports re-read instead, so the tripwire survives
    # reload and genuinely observes an import-time call.
    monkeypatch.setattr(
        "veridex.seed.official_replay_league.run_seed", _run_seed_tripwire
    )
    monkeypatch.setattr(
        "veridex.api.server.create_server_app", _build_app_tripwire
    )

    # A fresh import must not build the app or run the seed (all real work is inside functions).
    module = importlib.reload(cli)

    assert seed_called == []
    assert app_built == []
    assert callable(module.main)
    assert callable(module.build_app_and_store)


class _RecordingLifespan:
    """A fake ``router.lifespan_context`` that records enter/exit order into a shared list."""

    def __init__(self, order: list[str]) -> None:
        self._order = order

    def __call__(self, app: Any) -> _RecordingLifespan:
        return self

    async def __aenter__(self) -> None:
        self._order.append("enter")

    async def __aexit__(self, *_exc_info: Any) -> None:
        self._order.append("exit")


class _FakeRouter:
    def __init__(self, lifespan_context: Any) -> None:
        self.lifespan_context = lifespan_context


class _FakeAppWithLifespan:
    """Mirrors the attribute path ``_run`` reads: ``app.router.lifespan_context``."""

    def __init__(self, router: Any) -> None:
        self.router = router


def test_run_enters_app_lifespan_around_run_seed(monkeypatch: Any) -> None:
    order: list[str] = []
    fake_app = _FakeAppWithLifespan(_FakeRouter(_RecordingLifespan(order)))
    fake_store = object()
    monkeypatch.setattr(
        cli, "build_app_and_store", lambda: (fake_app, fake_store)
    )

    async def fake_run_seed(*_args: Any, **_kwargs: Any) -> SeedResult:
        order.append("run")
        return _empty_result("r1")

    monkeypatch.setattr(cli, "run_seed", fake_run_seed)

    cli.main(["--seed-revision", "r1"])

    # The production lifespan branch (``async with lifespan(app):``) must wrap run_seed so the
    # Postgres pool F1 relies on is open for the whole seed and closed only after it finishes.
    assert order == ["enter", "run", "exit"]


def test_seed_failure_exits_nonzero_with_message(monkeypatch: Any) -> None:
    sentinel_app = object()
    sentinel_store = object()
    monkeypatch.setattr(
        cli, "build_app_and_store", lambda: (sentinel_app, sentinel_store)
    )

    async def failing_run_seed(*_args: Any, **_kwargs: Any) -> SeedResult:
        raise SeedError("boom")

    monkeypatch.setattr(cli, "run_seed", failing_run_seed)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--seed-revision", "r1"])

    # Non-zero exit is preserved (SystemExit with a string code is fail-closed); the message is a
    # clean one-liner rather than a full traceback, for tidy F1 operator logs.
    assert "seed failed: boom" in str(exc_info.value)
