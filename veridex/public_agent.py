"""Immutable public-identity model for the Official Replay League completion layer (A1).

This module owns the public-facing agent identity: a two-state visibility flag, the operator
class (official vs user), and an honest ``Origin`` whose default is ``UNKNOWN`` — legacy agents
are never silently promoted to ``STUDIO``.

HONESTY INVARIANT (load-bearing): ``owner_public_label`` NEVER emits the raw ``owner_ref``. The
ref may be a Privy DID or an internal operator id, so it must not leak into any public response.
Official agents render a static brand string; user agents render a SHORTENED wallet derived from
the ref; anything else renders an em-dash.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel

_BRAND_LABEL = "Veridex Labs"
_DASH = "—"
_WALLET_RE = re.compile(r"0x[0-9a-fA-F]{6,}")


class Visibility(str, Enum):
    """Whether a public agent is discoverable. Two states only — there is no ``unlisted``."""

    PRIVATE = "private"
    PUBLIC = "public"


class OperatorClass(str, Enum):
    """Who runs the agent: a first-party official operator, or an external user."""

    OFFICIAL = "official"
    USER = "user"


class Origin(str, Enum):
    """How the agent came to exist. ``UNKNOWN`` is the honest default for legacy rows — never guess ``STUDIO``."""

    OFFICIAL = "official"
    STUDIO = "studio"
    BYOA = "byoa"
    UNKNOWN = "unknown"


class PublicAgent(BaseModel):
    """The immutable public identity of a deployed agent.

    Attributes:
        public_agent_id: Stable public identifier for the agent.
        display_name: Human-facing name shown in the arena.
        operator_class: Whether the agent is run by an official operator or a user.
        origin: How the agent originated (honest ``UNKNOWN`` for legacy rows).
        visibility: Two-state discoverability flag.
        owner_ref: Internal owner reference (Privy DID / operator id). NEVER serialized to a
            public response — see ``owner_public_label`` for the safe rendering.
        created_at: ISO-8601 creation timestamp.
        updated_at: ISO-8601 last-update timestamp.
        version: Monotonic identity version.
    """

    public_agent_id: str
    display_name: str
    operator_class: OperatorClass
    origin: Origin
    visibility: Visibility
    owner_ref: str | None
    created_at: str
    updated_at: str
    version: int = 1


def owner_public_label(agent: PublicAgent) -> str:
    """Render a safe, public owner label — NEVER the raw ``owner_ref``.

    Official agents render a static brand string. User agents render a shortened wallet extracted
    from ``owner_ref`` (``0x`` + at least 6 hex chars); a wallet longer than 11 chars is truncated
    to ``0xabcd…wxyz9``, otherwise the wallet is returned as-is. Anything else — a non-official
    agent with no embeddable wallet — renders an em-dash.
    """
    if agent.operator_class is OperatorClass.OFFICIAL:
        return _BRAND_LABEL
    ref = agent.owner_ref
    if ref is None:
        return _DASH
    match = _WALLET_RE.search(ref)
    if match is None:
        return _DASH
    wallet = match.group(0)
    if len(wallet) > 11:
        return f"{wallet[:6]}…{wallet[-5:]}"
    return wallet
