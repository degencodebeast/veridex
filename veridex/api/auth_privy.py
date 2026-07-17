"""Privy ES256 auth boundary for ``POST /agents/deploy`` (I-1; ``auth-contract@1``).

The frontend obtains a Privy access token via ``getAccessToken()`` and sends it as
``Authorization: Bearer <token>``. This module verifies that token **server-side** and derives the
owner identity from its signed claims — a client-supplied owner is never trusted.

Verification facts:

* Privy access tokens are **ES256** (ECDSA P-256) JWTs.
* Asserted claims: ``iss == "privy.io"``, ``aud == <PRIVY_APP_ID>``, a valid ``exp``, and ``sub``
  is the user's Privy DID (``did:privy:...``).
* The verification key is the **static SPKI/PEM** public key copied from the Privy dashboard —
  PyJWT's ``jwt.decode(..., algorithms=["ES256"])`` accepts that PEM directly, so there is NO
  network/JWKS fetch on the request path.
* PyJWT raises subclasses of ``jwt.InvalidTokenError`` for every validation failure
  (``InvalidSignatureError``/``ExpiredSignatureError``/``InvalidAudienceError``/``InvalidIssuerError``/
  ``DecodeError`` all inherit it), so catching the base is exhaustive.

This is the AUTH BOUNDARY only: it derives the principal. Persisting the owner onto the
``AgentInstance`` (I-2) and recording a ``DeploymentAttempt`` (I-3) are separate tasks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

import jwt
from fastapi import Header, HTTPException
from pydantic import BaseModel

if TYPE_CHECKING:
    from veridex.config import Settings

_PRIVY_ISSUER = "privy.io"
_DID_PREFIX = "did:privy:"


class PrivyPrincipal(BaseModel):
    """The server-derived authenticated principal (from the token's signed claims, never the body).

    Attributes:
        did: The user's Privy DID (the ``sub`` claim, ``did:privy:...``) — the owner identity.
        session_id: The Privy session id (``sid`` claim), when present.
    """

    did: str
    session_id: str | None = None


def verify_privy_token(token: str, *, app_id: str | None, verification_key: str | None) -> PrivyPrincipal:
    """Verify a Privy ES256 access token and return the derived :class:`PrivyPrincipal`.

    Fail-closed: any signature/audience/issuer/expiry failure, a missing required claim, or a
    ``sub`` that is not a Privy DID raises ``HTTPException(401)``.

    Args:
        token: The raw JWT from the ``Authorization: Bearer`` header.
        app_id: The expected ``aud`` (this deployment's Privy app id).
        verification_key: The static SPKI/PEM ES256 public key from the Privy dashboard.

    Returns:
        The verified principal whose ``did`` is the token's ``sub``.

    Raises:
        HTTPException: 401 on any verification failure or unconfigured verifier.
    """
    if not app_id or not verification_key:
        # No verifier material configured → cannot prove authenticity → fail closed.
        raise HTTPException(status_code=401, detail="privy auth not configured")
    try:
        claims = jwt.decode(
            token,
            verification_key,
            algorithms=["ES256"],
            audience=app_id,
            issuer=_PRIVY_ISSUER,
            # Require the trust-bearing claims to be PRESENT (not merely well-formed if present).
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        # InvalidTokenError is the base of every PyJWT validation failure (bad signature, wrong
        # aud/iss, expired, malformed) — one catch covers them all. No claim from an unverified token.
        raise HTTPException(status_code=401, detail="invalid privy access token") from exc
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.startswith(_DID_PREFIX):
        raise HTTPException(status_code=401, detail="invalid privy subject")
    sid = claims.get("sid")
    return PrivyPrincipal(did=sub, session_id=sid if isinstance(sid, str) else None)


class _Verifier(Protocol):
    """The injectable token-verifier seam (defaults to :func:`verify_privy_token`)."""

    def __call__(self, token: str, *, app_id: str | None, verification_key: str | None) -> PrivyPrincipal: ...


def make_require_principal(
    settings: Settings, verifier: _Verifier = verify_privy_token
) -> Callable[..., PrivyPrincipal]:
    """Build the FastAPI ``require_principal`` dependency bound to ``settings`` (fail-closed).

    In ``AUTH_MODE=privy`` a valid Privy bearer token is REQUIRED — a missing/malformed header 401s
    BEFORE the verifier is consulted, and the verifier 401s on any invalid token. In ``AUTH_MODE=dev``
    (local dev only; hard-refused in production by :class:`~veridex.config.Settings`) the dependency
    yields a fixed dev principal without requiring a token.

    Args:
        settings: The resolved application settings (auth mode + Privy app id / verification key).
        verifier: The token verifier (injectable for tests); defaults to :func:`verify_privy_token`.

    Returns:
        A FastAPI dependency callable resolving to a :class:`PrivyPrincipal`.
    """

    def require_principal(authorization: str | None = Header(default=None)) -> PrivyPrincipal:  # noqa: B008
        if settings.auth_mode == "dev":
            return PrivyPrincipal(did="did:privy:dev")
        # RFC 6750 defines the auth scheme token as case-insensitive: accept Bearer/bearer/BEARER.
        scheme, _, credential = (authorization or "").partition(" ")
        token = credential.strip()
        if scheme.casefold() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="missing or malformed privy bearer token")
        return verifier(token, app_id=settings.privy_app_id, verification_key=settings.privy_verification_key)

    return require_principal
