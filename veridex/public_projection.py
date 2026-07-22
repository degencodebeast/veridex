"""B1 — sealed→public projection adapter for the Official Replay League completion layer.

The directional leaderboard is empty because nothing feeds the pure aggregator
:func:`veridex.leaderboard.leaderboard` a durable, stable-identity list of score rows.
``leaderboard()`` groups ONLY on ``record["agent_id"]`` — it never inspects a public id.

This adapter maps each SEALED per-run score row (keyed by the *runtime* agent id, from
:func:`veridex.scoring.score_run`) to a PUBLIC row whose leaderboard-input ``agent_id``
HOLDS the ``public_agent_id`` — so the unchanged aggregator groups by the public id —
carrying replay provenance, WITHOUT mutating the sealed row.

FAIL-CLOSED (trust surface): a sealed row whose runtime agent id has no binding is a
provenance gap, so :func:`project_public_rows` raises :class:`ProjectionError` rather than
silently dropping or guessing the public identity.
"""

from __future__ import annotations

import copy
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from veridex.leaderboard import leaderboard
from veridex.public_agent import OperatorClass, Visibility

if TYPE_CHECKING:
    from veridex.store import Store

_VALID_SOURCE_MODES = ("replay", "live")


class ProjectionError(Exception):
    """Raised when a sealed row cannot be projected to a public row (fail-closed)."""


class PublicBinding(BaseModel):
    """Maps a runtime agent id to its stable public identity and run context.

    Keyed in the ``bindings`` mapping by the *runtime* agent id (the sealed row's
    ``agent_id``); ``public_agent_id`` becomes the projected row's ``agent_id`` so the
    unchanged aggregator groups under the public id.
    """

    public_agent_id: str
    instance_id: str
    config_hash: str


def project_public_rows(
    sealed_rows: list[dict[str, Any]],
    bindings: dict[str, PublicBinding],
    *,
    run_id: str,
    source_mode: Literal["replay", "live"],
) -> list[dict[str, Any]]:
    """Project sealed per-run score rows into public leaderboard-input rows.

    For each sealed row, looks up ``bindings[row["agent_id"]]`` (key = runtime agent id)
    and emits a DEEP COPY of the row with ``agent_id`` REPLACED by the binding's
    ``public_agent_id`` (so the unchanged aggregator groups by the public id), plus added
    provenance keys: ``public_agent_id``, ``runtime_agent_id``, ``instance_id``,
    ``config_hash``, ``run_id``, ``source_mode``.

    Args:
        sealed_rows: Per-run ``score_run`` output rows, each keyed by the runtime agent id.
        bindings: Runtime-agent-id → :class:`PublicBinding` mapping.
        run_id: The run these rows came from (provenance).
        source_mode: ``"replay"`` or ``"live"``.

    Returns:
        One projected public row per sealed row, in input order.

    Raises:
        ProjectionError: A sealed row's runtime agent id has no binding (fail-closed), or
            ``source_mode`` is not ``"replay"``/``"live"`` (defensive guard).
    """
    if source_mode not in _VALID_SOURCE_MODES:
        raise ProjectionError(
            f"invalid source_mode {source_mode!r}; expected one of {_VALID_SOURCE_MODES}"
        )

    public_rows: list[dict[str, Any]] = []
    for row in sealed_rows:
        runtime_agent_id = row["agent_id"]
        binding = bindings.get(runtime_agent_id)
        if binding is None:
            raise ProjectionError(
                f"no public binding for runtime agent id {runtime_agent_id!r} "
                "(fail-closed: refusing to project an unbound sealed row)"
            )

        public_row = copy.deepcopy(row)
        public_row["agent_id"] = binding.public_agent_id
        public_row["public_agent_id"] = binding.public_agent_id
        public_row["runtime_agent_id"] = runtime_agent_id
        public_row["instance_id"] = binding.instance_id
        public_row["config_hash"] = binding.config_hash
        public_row["run_id"] = run_id
        public_row["source_mode"] = source_mode
        public_rows.append(public_row)

    return public_rows


class BoardKind(str, Enum):
    """Which directional board to read — the OFFICIAL benchmark, or all public agents.

    ``OFFICIAL_BENCHMARK`` keeps only agents whose ``operator_class`` is
    :attr:`~veridex.public_agent.OperatorClass.OFFICIAL`; ``PUBLIC_AGENTS`` keeps every
    publicly-visible agent regardless of operator class. Both drop non-public agents.
    """

    OFFICIAL_BENCHMARK = "official_benchmark"
    PUBLIC_AGENTS = "public_agents"


async def directional_board(store: Store, *, board_kind: BoardKind) -> list[dict[str, Any]]:
    """Read a directional leaderboard, joining CURRENT public-agent visibility at read time.

    Loads every durable projected row (:meth:`~veridex.store.Store.list_projected_rows`) and, for
    each, resolves its owning public agent by ``public_agent_id``. A row is DROPPED when its agent
    is absent or not :attr:`~veridex.public_agent.Visibility.PUBLIC` — visibility is joined LIVE,
    so flipping an agent private hides it WITHOUT deleting its stored rows. For
    :attr:`BoardKind.OFFICIAL_BENCHMARK` a row is additionally kept only when its agent's
    ``operator_class`` is :attr:`~veridex.public_agent.OperatorClass.OFFICIAL`.

    The surviving rows are handed UNCHANGED to :func:`veridex.leaderboard.leaderboard`, which groups
    on ``agent_id`` (== the public id, as B1 emits) and pools per-agent across runs.

    Args:
        store: The durable store to read projected rows and public-agent identities from.
        board_kind: Which board to build — official benchmark only, or all public agents.

    Returns:
        The aggregated, ranked leaderboard rows for the kept agents (may be empty).
    """
    rows = await store.list_projected_rows()
    kept: list[dict[str, Any]] = []
    for row in rows:
        agent = await store.get_public_agent(row["public_agent_id"])
        if agent is None or agent.visibility is not Visibility.PUBLIC:
            continue
        if board_kind is BoardKind.OFFICIAL_BENCHMARK and agent.operator_class is not OperatorClass.OFFICIAL:
            continue
        kept.append(row)
    return leaderboard(kept)
