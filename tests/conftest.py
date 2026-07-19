"""Shared pytest fixtures.

R-4: production replay loads a verified ReplayPack from the R-2 catalog — never the synthetic
``build_demo_ticks`` fixture. In production ``REPLAY_PACK_ROOT`` is ALWAYS mounted (Dockerfile /
compose bind the R-1 banked genuine seed at the ``curated`` leaf), so ``create_app`` builds a
non-empty catalog. Tests that construct ``create_app``/``create_server_app`` WITHOUT injecting their
own ``replay_catalog`` should see the same non-empty catalog rather than the fail-closed empty one.

This autouse fixture points ``REPLAY_PACK_ROOT`` at the bundled seed pack for every test, mirroring
production. It uses ``monkeypatch`` so it is per-test isolated: a test that manages its own
``REPLAY_PACK_ROOT`` (or injects an explicit ``replay_catalog``) still wins — ``create_app`` reads the
env ONLY when no catalog is injected, and a later ``monkeypatch.setenv`` in the test body overrides
this default and is restored on teardown.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The R-1 banked GENUINE seed pack (the single curated production pack; catalogs as one entry, so an
# unbound competition/deploy auto-selects it). Mirrors the Dockerfile/compose ``curated`` mount.
_SEED_PACK_DIR = Path(__file__).resolve().parents[1] / "scripts" / "fixtures" / "demo_pack_real"


# Docker-compose integration tests interpolate the HOST environment into the container (shell env wins
# over ``--env-file``). A host ``REPLAY_PACK_ROOT`` would override their compose default and point the
# container's read-only curated mount at a host-only path. Identify those tests by their compose/DB
# stack fixtures and leave their environment untouched — they manage their own env.
_STACK_FIXTURES = frozenset({"compose_stack", "postgres_stack"})


@pytest.fixture(autouse=True)
def _replay_pack_root(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``REPLAY_PACK_ROOT`` at the bundled seed pack (mirrors the always-mounted prod curated root).

    Skips docker-compose integration tests (whose subprocess would inherit and mis-apply the host env).
    In-process ``create_app``/``register_deploy_routes`` read this ONLY when no ``replay_catalog`` is
    injected, so a test that injects its own catalog is unaffected either way.
    """
    if _STACK_FIXTURES & set(request.fixturenames):
        return
    monkeypatch.setenv("REPLAY_PACK_ROOT", str(_SEED_PACK_DIR))
