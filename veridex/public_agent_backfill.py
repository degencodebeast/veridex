"""A3 — backfill every legacy deployed instance to a distinct PRIVATE :class:`PublicAgent`.

The Official Replay League completion layer needs every deployed :class:`AgentInstance` to carry a
public identity. Instances deployed BEFORE the public-agent layer existed have none, so this pass
mints one — the HONEST legacy default (load-bearing):

* ``origin=UNKNOWN`` — we do not know how a legacy instance originated, so we NEVER guess ``STUDIO``/
  ``BYOA``;
* ``operator_class=USER`` and ``visibility=PRIVATE`` — a pre-existing deployment must never leak into
  the public directory;
* ``owner_ref=instance.operator_id`` and ``display_name=instance.agent_id`` — carried verbatim, no
  fabrication.

The pass is IDEMPOTENT (already-linked instances are skipped) and takes an INJECTED ``mint_id`` so
callers/tests control id generation — there is no ``uuid``/``random`` inside the function.

CRASH-SAFETY (load-bearing): ``mint_id`` is DETERMINISTIC per instance — it takes the ``instance_id``
and returns the SAME ``public_agent_id`` every time for that instance. This makes the two writes
(persist + link) recoverable without a cross-op transaction: if the process stops AFTER
``persist_public_agent`` but BEFORE ``link_instance_public_agent``, a retry re-derives the SAME id, so
``persist_public_agent`` (an UPSERT keyed by ``public_agent_id``) overwrites the orphaned row instead
of minting a second one, and the link is then completed. Net after any crash+retry: exactly ONE
:class:`PublicAgent` and ONE durable link per instance — no orphan, no duplicate (AC-2).
"""

from __future__ import annotations

from collections.abc import Callable

from veridex.public_agent import OperatorClass, Origin, PublicAgent, Visibility
from veridex.store import Store


async def backfill_public_agents(store: Store, *, now: str, mint_id: Callable[[str], str]) -> int:
    """Mint one distinct PRIVATE :class:`PublicAgent` for every un-linked deployed instance.

    For each instance from :meth:`Store.list_agent_instances` that is not already linked to a public
    agent, derive its ``public_agent_id`` via ``mint_id(instance.instance_id)``, persist an honest
    legacy :class:`PublicAgent` (see module docstring for the invariants), then durably link the
    instance to it. Instances already linked are skipped, making the pass idempotent.

    Args:
        store: The durable store to read instances from and persist/link public agents to.
        now: ISO-8601 UTC timestamp used for both ``created_at`` and ``updated_at``.
        mint_id: Injected DETERMINISTIC factory mapping an ``instance_id`` to its ``public_agent_id``
            — the SAME ``instance_id`` MUST yield the SAME id (so a crash+retry re-derives it and the
            UPSERT reuses the row, never duplicating), and DISTINCT instances MUST yield DISTINCT ids.

    Returns:
        The number of public agents minted (0 on a fully-backfilled store).
    """
    minted = 0
    for instance in await store.list_agent_instances():
        if await store.get_instance_public_agent_id(instance.instance_id) is not None:
            continue
        pid = mint_id(instance.instance_id)
        await store.persist_public_agent(
            PublicAgent(
                public_agent_id=pid,
                display_name=instance.agent_id,
                operator_class=OperatorClass.USER,
                origin=Origin.UNKNOWN,
                visibility=Visibility.PRIVATE,
                owner_ref=instance.operator_id,
                created_at=now,
                updated_at=now,
                version=1,
            )
        )
        await store.link_instance_public_agent(instance.instance_id, pid)
        minted += 1
    return minted
