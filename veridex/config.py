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

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Tuning
    # ------------------------------------------------------------------
    decision_timeout_s: float = 30.0

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
