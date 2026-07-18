"""Typed configuration module for Veridex (Bit Bcfg — Phase 1).

Centralises all environment-backed config in one :class:`Settings` object.
This module is **SHELL-only** (CON-010): the deterministic trust core must
never import it so the offline test suite remains completely env-free.

Usage in the async shell layer::

    from veridex.config import get_settings, require_txline

    settings = get_settings()
    jwt, token = require_txline(settings)
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from veridex.dust_execution.privy_provisioning import PinnedProvisioningConfig

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Only these EXPLICIT values are treated as non-production for the auth-boundary guard. Any other
# value — including ``prod``, ``production``, ``staging``, typos, or unknown envs — is treated as
# PRODUCTION (strict, fail-closed default) so a misspelled ``APP_ENV`` can never silently re-enable
# the ``AUTH_MODE=dev`` bypass in a live deployment.
_NON_PRODUCTION_APP_ENVS = frozenset({"development", "dev", "test", "local"})


class Settings(BaseSettings):
    """Typed, env-backed configuration for Veridex.

    All secrets default to ``None`` so ``Settings()`` always constructs
    successfully with zero env vars — the offline test suite never needs live
    credentials.

    Env-file source: ``veridex/.env`` (skipped when ``_env_file=None`` is
    passed to the constructor, as done in every test).

    Args:
        BaseSettings: pydantic-settings v2 base class for env-backed settings.
    """

    model_config = SettingsConfigDict(
        env_file="veridex/.env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # ------------------------------------------------------------------
    # TxLINE (EXT-001 devnet endpoint)
    # ------------------------------------------------------------------
    txline_base_url: str = "https://txline-dev.txodds.com/api"
    txline_jwt: str | None = Field(default=None, validation_alias="JWT")
    txline_api_token: str | None = Field(default=None, validation_alias="TXLINE_X_API_TOKEN")
    # Auth host for guest-JWT / token-activate (CON-041); the ``/api`` base stays separate.
    txline_auth_base_url: str = "https://txline-dev.txodds.com"
    # Program id for the on-chain ``subscribe()`` tx; secret-by-policy, defaults ``None``.
    txline_subscribe_program_id: str | None = Field(default=None, validation_alias="TXLINE_SUBSCRIBE_PROGRAM_ID")

    # ------------------------------------------------------------------
    # Solana
    # ------------------------------------------------------------------
    solana_rpc_url: str = "https://api.devnet.solana.com"
    solana_cluster: str = "devnet"
    solana_keypair_path: str | None = Field(default=None, validation_alias="SOLANA_KEYPAIR_PATH")

    # ------------------------------------------------------------------
    # Postgres
    # ------------------------------------------------------------------
    database_url: str | None = Field(default=None, validation_alias="DATABASE_URL")

    # ------------------------------------------------------------------
    # Agent (§8 deps)
    # ------------------------------------------------------------------
    # OpenRouter ``provider/model`` slug (override via MODEL_ID env var).
    model_id: str = "anthropic/claude-sonnet-4"
    # OpenRouter gateway key (primary LLM path).
    openrouter_api_key: str | None = Field(default=None, validation_alias="OPENROUTER_API_KEY")
    # Anthropic direct key — back-compat; no longer the default agent path.
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")

    # ------------------------------------------------------------------
    # Control-plane operator auth (Phase-2B Task 7; async shell, CON-010)
    # ------------------------------------------------------------------
    # Bearer token required for control-plane WRITES (start non-paper / approve /
    # kill-switch). ``None`` means no operator is configured and every write fails closed.
    operator_token: str | None = Field(default=None, validation_alias="OPERATOR_TOKEN")
    # Identifier of the authenticated operator principal; compared against a competition's
    # ``operator_id`` for per-competition ownership (403 on mismatch).
    operator_id: str | None = Field(default=None, validation_alias="OPERATOR_ID")

    # ------------------------------------------------------------------
    # Privy access-token auth boundary (POST /agents/deploy; I-1, auth-contract@1)
    # ------------------------------------------------------------------
    # Deployment environment gate. ``production`` HARD-REFUSES the ``dev`` auth bypass (below) and
    # requires the Privy verifier material to be present (see the model validator).
    app_env: str = Field(default="development", validation_alias="APP_ENV")
    # ``privy`` verifies a real Privy ES256 access token on every deploy; ``dev`` is the local-dev
    # bypass (refused in production). Fail-closed default is ``dev`` only OUTSIDE production.
    auth_mode: Literal["privy", "dev"] = Field(default="dev", validation_alias="AUTH_MODE")
    # This deployment's Privy app id — asserted as the ``aud`` claim of the access token.
    privy_app_id: str | None = Field(default=None, validation_alias="PRIVY_APP_ID")
    # The static SPKI/PEM ES256 verification key copied from the Privy dashboard (no network/JWKS).
    privy_verification_key: str | None = Field(default=None, validation_alias="PRIVY_VERIFICATION_KEY")

    # ------------------------------------------------------------------
    # Privy execution-wallet provisioning (II-5b; CODE_READY / CLAIM_WITHHELD)
    # ------------------------------------------------------------------
    # Master gate. OFF by default: with provisioning disabled the deploy saga behaves exactly as
    # II-5 (no wallet is provisioned). The wallet CLAIM stays withheld until the operator supplies
    # the pinned policy/quorum config below AND runs Level-3 acceptance (with the II-5c adapter).
    privy_provisioning_enabled: bool = Field(default=False, validation_alias="PRIVY_PROVISIONING_ENABLED")
    # The EXACT operator-reviewed Privy policy + key-quorum resource ids the binding is admitted
    # against (non-secret ids). Values are operator-supplied — never invented in code.
    privy_execution_policy_id: str | None = Field(default=None, validation_alias="PRIVY_EXECUTION_POLICY_ID")
    privy_execution_quorum_id: str | None = Field(default=None, validation_alias="PRIVY_EXECUTION_QUORUM_ID")
    # The pinned content hashes (PrivyWalletPolicy.content_hash / AuthorizationQuorum.content_hash) +
    # threshold the live resources must match exactly (drift → fail closed).
    privy_execution_policy_content_hash: str | None = Field(
        default=None, validation_alias="PRIVY_EXECUTION_POLICY_CONTENT_HASH"
    )
    # OPTIONAL full-fidelity policy hash (covers per-field policy conditions the coarse content hash
    # cannot see). Once the operator authors a real policy with field-level conditions they SHOULD pin
    # this for full-fidelity drift detection; absent, admission relies on the coarse hash + reject-unmodeled.
    privy_execution_policy_full_content_hash: str | None = Field(
        default=None, validation_alias="PRIVY_EXECUTION_POLICY_FULL_CONTENT_HASH"
    )
    privy_execution_quorum_content_hash: str | None = Field(
        default=None, validation_alias="PRIVY_EXECUTION_QUORUM_CONTENT_HASH"
    )
    privy_execution_quorum_threshold: int | None = Field(
        default=None, validation_alias="PRIVY_EXECUTION_QUORUM_THRESHOLD"
    )
    # The wallet owner id (the pinned quorum); defaults to the quorum id when omitted.
    privy_execution_owner_id: str | None = Field(default=None, validation_alias="PRIVY_EXECUTION_OWNER_ID")
    # A NON-SECRET reference to the request-authorization key. The P-256 key material itself lives
    # ONLY in the secret manager and is consumed by the II-5c HTTP adapter — never here.
    privy_execution_authorization_key_id: str | None = Field(
        default=None, validation_alias="PRIVY_EXECUTION_AUTHORIZATION_KEY_ID"
    )

    # ------------------------------------------------------------------
    # Tuning
    # ------------------------------------------------------------------
    decision_timeout_s: float = 30.0

    @property
    def is_production(self) -> bool:
        """True unless ``app_env`` is an EXPLICIT non-production value (fail-closed for the auth guard).

        ``app_env`` is normalized (``strip().casefold()``) and matched against
        :data:`_NON_PRODUCTION_APP_ENVS`; ANY other/unrecognized value — ``prod``, ``production``,
        ``staging``, trailing whitespace, typos — is treated as production so the ``AUTH_MODE=dev``
        bypass can never leak into a live deployment through a misspelled env.
        """
        return self.app_env.strip().casefold() not in _NON_PRODUCTION_APP_ENVS

    @model_validator(mode="after")
    def _enforce_production_auth_boundary(self) -> Settings:
        """Fail-closed startup guard: ``production`` refuses the dev bypass and missing Privy creds.

        ``AUTH_MODE=dev`` MUST NOT run in a production-equivalent ``APP_ENV`` (it would deploy without
        verifying a real Privy token), and a production + ``privy`` config MUST carry both the Privy
        app id and the verification key (else every deploy would fail closed at request time). Both
        raise at construction time so a misconfigured production process refuses to start rather than
        degrade. "Production-equivalent" is decided fail-closed by :attr:`is_production`.
        """
        if self.is_production:
            if self.auth_mode == "dev":
                raise ValueError("AUTH_MODE=dev is refused when APP_ENV is production (fail-closed auth boundary)")
            if self.privy_app_id is None or self.privy_verification_key is None:
                raise ValueError("A production APP_ENV requires PRIVY_APP_ID and PRIVY_VERIFICATION_KEY")
        return self

    @model_validator(mode="after")
    def _enforce_provisioning_pins(self) -> Settings:
        """Fail-closed STARTUP guard: enabling II-5b provisioning requires the FULL pinned custody config.

        Without this, ``PRIVY_PROVISIONING_ENABLED=true`` could be accepted with pins absent and the
        custody contract would only be checked later at launch — an "enabled but unpinned" fail-open
        window. Validating at construction makes a misconfigured process refuse to start. Mirrors the
        fail-closed intent of :meth:`require_privy_provisioning` (kept as defense-in-depth at resolution).
        """
        if not self.privy_provisioning_enabled:
            return self
        required = {
            "PRIVY_EXECUTION_POLICY_ID": self.privy_execution_policy_id,
            "PRIVY_EXECUTION_QUORUM_ID": self.privy_execution_quorum_id,
            "PRIVY_EXECUTION_POLICY_CONTENT_HASH": self.privy_execution_policy_content_hash,
            "PRIVY_EXECUTION_POLICY_FULL_CONTENT_HASH": self.privy_execution_policy_full_content_hash,
            "PRIVY_EXECUTION_QUORUM_CONTENT_HASH": self.privy_execution_quorum_content_hash,
            "PRIVY_EXECUTION_QUORUM_THRESHOLD": self.privy_execution_quorum_threshold,
            "PRIVY_EXECUTION_AUTHORIZATION_KEY_ID": self.privy_execution_authorization_key_id,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(
                f"PRIVY_PROVISIONING_ENABLED requires the pinned custody config; missing: {', '.join(missing)}"
            )
        if self.privy_execution_owner_id is not None and self.privy_execution_owner_id != self.privy_execution_quorum_id:
            raise ValueError("PRIVY_EXECUTION_OWNER_ID must equal PRIVY_EXECUTION_QUORUM_ID (the wallet owner is the verified quorum)")
        return self

    # ------------------------------------------------------------------
    # SX Bet venue adapter (async shell; CON-010)
    # ------------------------------------------------------------------
    # Set to true to enable the live SX Bet path (requires maker + key below).
    sx_bet_enabled: bool = Field(default=False, validation_alias="SX_BET_ENABLED")
    # SX Bet REST base URL.  Defaults to testnet (safe for Phase-2B guarded runs).
    # Override to https://api.sx.bet for mainnet (chainId 4162) via SX_BET_BASE_URL.
    sx_bet_base_url: str = Field(default="https://api.toronto.sx.bet", validation_alias="SX_BET_BASE_URL")
    # EVM wallet address used as the maker on SX Bet (EIP-712 signing).
    sx_bet_maker_address: str | None = Field(default=None, validation_alias="SX_BET_MAKER_ADDRESS")
    # Private key for EIP-712 order signing — NEVER commit this value.
    sx_bet_private_key: str | None = Field(default=None, validation_alias="SX_BET_PRIVATE_KEY")

    # ------------------------------------------------------------------
    # Polymarket venue adapter (async shell; CON-010)
    # ------------------------------------------------------------------
    # Polymarket CLOB is MAINNET real money, so the write path is DISABLED by default:
    # submit_order / cancel_order raise PolymarketWriteDisabled until this is explicitly true.
    # The READ path (depth-aware quotes) is always available and needs no credential.
    polymarket_write_enabled: bool = Field(default=False, validation_alias="POLYMARKET_WRITE_ENABLED")


# ---------------------------------------------------------------------------
# Fail-fast-at-use helpers
# ---------------------------------------------------------------------------


def require_txline(settings: Settings) -> tuple[str, str]:
    """Return ``(jwt, api_token)`` or raise if either TxLINE credential is missing.

    Secrets are validated only when a live TxLINE operation is attempted, so the
    offline suite never triggers this guard.

    Args:
        settings: Application settings instance.

    Returns:
        A ``(jwt, api_token)`` tuple of credential strings.

    Raises:
        ValueError: If either ``txline_jwt`` or ``txline_api_token`` is ``None``.
    """
    if settings.txline_jwt is None or settings.txline_api_token is None:
        raise ValueError("TxLINE creds missing: set JWT and TXLINE_X_API_TOKEN in veridex/.env")
    return settings.txline_jwt, settings.txline_api_token


def require_txline_subscribe(settings: Settings) -> tuple[str, str]:
    """Return ``(keypair_path, program_id)`` for the on-chain subscribe tx, or raise.

    The on-chain ``subscribe()`` (CON-041) needs both a Solana keypair to sign the
    free World-Cup subscribe tx and the target program id. Both come from typed
    config only; the guard fires only when a live subscribe is attempted, so the
    offline suite never triggers it.

    Args:
        settings: Application settings instance.

    Returns:
        A ``(keypair_path, program_id)`` tuple.

    Raises:
        ValueError: If either ``solana_keypair_path`` or
            ``txline_subscribe_program_id`` is ``None``.
    """
    if settings.solana_keypair_path is None or settings.txline_subscribe_program_id is None:
        raise ValueError("subscribe creds missing: set SOLANA_KEYPAIR_PATH and TXLINE_SUBSCRIBE_PROGRAM_ID")
    return settings.solana_keypair_path, settings.txline_subscribe_program_id


def require_privy_provisioning(settings: Settings) -> PinnedProvisioningConfig | None:
    """Resolve the pinned II-5b provisioning config, or ``None`` when provisioning is DISABLED.

    Fail-closed: when ``privy_provisioning_enabled`` is true, EVERY pinned value must be present —
    a missing policy id / quorum id / content hash / threshold / authorization-key ref raises rather
    than provisioning against an incomplete custody contract. When the gate is off, returns ``None``
    (the deploy saga then provisions no wallet — exactly the II-5 behavior).

    Args:
        settings: Application settings instance.

    Returns:
        A ``PinnedProvisioningConfig`` when enabled + fully configured, else ``None``.

    Raises:
        ValueError: If provisioning is enabled but any pinned value is absent.
    """
    if not settings.privy_provisioning_enabled:
        return None
    # Lazy import keeps ``config`` free of a hard dependency on the dust-execution package at import
    # time (the shell may load settings without the provisioning core present).
    from veridex.dust_execution.privy_provisioning import PinnedProvisioningConfig  # noqa: PLC0415

    policy_id = settings.privy_execution_policy_id
    quorum_id = settings.privy_execution_quorum_id
    policy_hash = settings.privy_execution_policy_content_hash
    quorum_hash = settings.privy_execution_quorum_content_hash
    threshold = settings.privy_execution_quorum_threshold
    auth_key_id = settings.privy_execution_authorization_key_id
    # The full-fidelity policy hash is REQUIRED when provisioning is enabled — it is the only gate that
    # catches a field-level policy substitution the coarse content hash cannot see, so leaving it opt-in
    # would keep that hole open. Treat it like every other pinned value: fail closed if absent.
    full_hash = settings.privy_execution_policy_full_content_hash
    missing = [
        name
        for name, value in (
            ("PRIVY_EXECUTION_POLICY_ID", policy_id),
            ("PRIVY_EXECUTION_QUORUM_ID", quorum_id),
            ("PRIVY_EXECUTION_POLICY_CONTENT_HASH", policy_hash),
            ("PRIVY_EXECUTION_POLICY_FULL_CONTENT_HASH", full_hash),
            ("PRIVY_EXECUTION_QUORUM_CONTENT_HASH", quorum_hash),
            ("PRIVY_EXECUTION_QUORUM_THRESHOLD", threshold),
            ("PRIVY_EXECUTION_AUTHORIZATION_KEY_ID", auth_key_id),
        )
        if value is None
    ]
    if missing:
        raise ValueError(
            "PRIVY_PROVISIONING_ENABLED is set but the pinned custody config is incomplete; "
            f"missing: {', '.join(missing)}"
        )
    assert policy_id is not None and quorum_id is not None and policy_hash is not None
    assert quorum_hash is not None and threshold is not None and auth_key_id is not None
    # MAJOR-1 (custody invariant): the wallet OWNER must be THE pinned key quorum whose threshold /
    # keys / content-hash we verify — our Model-1 (agent-controlled, developer-owned) wallet has no
    # legitimate reason to be owned by any other quorum. Fail CLOSED if an operator pins a divergent
    # owner id, so the wallet is never admitted as owned by a quorum we never verified.
    owner_id = settings.privy_execution_owner_id
    if owner_id is not None and owner_id != quorum_id:
        raise ValueError(
            "PRIVY_EXECUTION_OWNER_ID must equal PRIVY_EXECUTION_QUORUM_ID (the wallet must be owned "
            f"by the pinned, verified quorum); got owner={owner_id!r} quorum={quorum_id!r}"
        )
    return PinnedProvisioningConfig(
        policy_id=policy_id,
        quorum_id=quorum_id,
        owner_id=owner_id or quorum_id,
        policy_content_hash=policy_hash,
        quorum_content_hash=quorum_hash,
        quorum_threshold=threshold,
        authorization_key_id=auth_key_id,
        policy_full_content_hash=full_hash,
    )


def require_database_url(settings: Settings) -> str:
    """Return the Postgres database URL or raise if not configured.

    Args:
        settings: Application settings instance.

    Returns:
        The ``DATABASE_URL`` connection string.

    Raises:
        ValueError: If ``database_url`` is ``None``.
    """
    if settings.database_url is None:
        raise ValueError("set DATABASE_URL for the Postgres store")
    return settings.database_url


def require_anthropic_key(settings: Settings) -> str:
    """Return the Anthropic API key or raise if not configured.

    Back-compat helper; the default agent path now uses OpenRouter via
    :func:`require_openrouter_key`.

    Args:
        settings: Application settings instance.

    Returns:
        The ``ANTHROPIC_API_KEY`` string.

    Raises:
        ValueError: If ``anthropic_api_key`` is ``None``.
    """
    if settings.anthropic_api_key is None:
        raise ValueError("set ANTHROPIC_API_KEY for the LLM agent")
    return settings.anthropic_api_key


def require_openrouter_key(settings: Settings) -> str:
    """Return the OpenRouter API key or raise if not configured.

    This is the primary credential for the B4 LLM agent, which routes all
    model calls through OpenRouter (``https://openrouter.ai/api/v1``) to
    support multi-model competition across Claude/GPT/Gemini/DeepSeek/etc.

    Args:
        settings: Application settings instance.

    Returns:
        The ``OPENROUTER_API_KEY`` string.

    Raises:
        ValueError: If ``openrouter_api_key`` is ``None``.
    """
    if settings.openrouter_api_key is None:
        raise ValueError("set OPENROUTER_API_KEY for the LLM agent")
    return settings.openrouter_api_key


def require_keypair_path(settings: Settings) -> str:
    """Return the Solana keypair file path or raise if not configured.

    Args:
        settings: Application settings instance.

    Returns:
        The filesystem path to the Solana keypair JSON file.

    Raises:
        ValueError: If ``solana_keypair_path`` is ``None``.
    """
    if settings.solana_keypair_path is None:
        raise ValueError("set SOLANA_KEYPAIR_PATH to the Solana keypair JSON file path")
    return settings.solana_keypair_path


def require_sx_bet(settings: Settings) -> tuple[str, str]:
    """Return ``(maker_address, private_key)`` or raise if SX Bet creds are missing.

    Secrets are validated only when a live SX Bet operation is attempted, so the
    offline suite never triggers this guard.

    Args:
        settings: Application settings instance.

    Returns:
        A ``(maker_address, private_key)`` tuple of credential strings.

    Raises:
        ValueError: If either ``sx_bet_maker_address`` or ``sx_bet_private_key`` is ``None``.
    """
    if settings.sx_bet_maker_address is None or settings.sx_bet_private_key is None:
        raise ValueError("SX Bet creds missing: set SX_BET_MAKER_ADDRESS and SX_BET_PRIVATE_KEY in veridex/.env")
    return settings.sx_bet_maker_address, settings.sx_bet_private_key


# ---------------------------------------------------------------------------
# Cached accessor
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton :class:`Settings` instance, created once and cached.

    Use :func:`get_settings.cache_clear` in tests when a fresh instance is
    required with different env vars.

    Returns:
        The cached :class:`Settings` instance.
    """
    return Settings()
