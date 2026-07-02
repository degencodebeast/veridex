"""Bundled REPLAY source for the default-app Studio deploy (REQ-2D-703).

The default mounted deploy route has NO test-injected ``DeployDeps.marketstates``. So that the
headline ``configure → preflight → deploy → observe → verify`` flow (AC-2D-702) is demonstrable
from the REAL app — not only under test injection — a ``replay``/``paper`` deploy sources its
ticks from a REAL bundled ReplayPack shipped inside the package (``bundled_replay.json``).

Honest labels (doctrine): this is RECORDED REPLAY demo data — it is NEVER live and never implies
real-money execution. It replays through the SAME normalizer live TxLINE uses (via
:func:`~veridex.ingest.marketstate.replay_marketstates`), and the run it produces is sealed and
self-verifies for real (the ``/runs/{id}/verify`` recompute is authoritative). No network, no LLM
on this path — the file is a static package resource.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from veridex.ingest.marketstate import MarketState, replay_marketstates

#: The package-relative bundled ReplayPack (recorded demo ticks; NEVER live).
BUNDLED_REPLAY_PATH: Path = Path(__file__).with_name("bundled_replay.json")


@lru_cache(maxsize=1)
def load_bundled_replay_marketstates() -> tuple[MarketState, ...]:
    """Load the bundled REPLAY demo ticks as ordered, deterministic ``MarketState`` snapshots.

    Cached: the bundled pack is a static package resource, so it is parsed once and reused across
    deploys. Callers that need a mutable list should wrap the result with ``list(...)``.

    Returns:
        The bundled pack's ticks as a tuple of :class:`~veridex.ingest.marketstate.MarketState`,
        replayed through the SAME normalizer the live loop uses.
    """
    return tuple(replay_marketstates(str(BUNDLED_REPLAY_PATH)))
