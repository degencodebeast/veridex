"""Tests for veridex.config — offline-deterministic (no real .env needed).

All tests construct Settings with ``_env_file=None`` and monkeypatch OS env
vars so the suite passes regardless of whether ``veridex/.env`` exists on disk.

TDD: each test was designed RED (feature absent) before the implementation in
``veridex/config.py`` was written.

Test groups:
- TestDefaults       — zero-env construction gives devnet URLs, None secrets.
- TestEnvOverrides   — monkeypatched env vars are picked up correctly.
- TestRequireTxline  — require_txline raises / returns correctly.
- TestRequireDatabaseUrl — require_database_url raises / returns correctly.
- TestRequireAnthropicKey — require_anthropic_key raises / returns correctly.
- TestRequireKeypairPath  — require_keypair_path raises / returns correctly.
- TestGetSettings    — get_settings() returns a cached Settings instance.
"""

from __future__ import annotations

import pytest

from veridex.config import (
    Settings,
    get_settings,
    require_anthropic_key,
    require_database_url,
    require_keypair_path,
    require_txline,
)

# Config env vars that must be cleared so tests are deterministic regardless of the
# developer's / CI's OS environment (e.g. an exported DATABASE_URL for the Postgres path).
_CONFIG_ENV_VARS = ("JWT", "TXLINE_X_API_TOKEN", "DATABASE_URL", "SOLANA_KEYPAIR_PATH", "ANTHROPIC_API_KEY")


@pytest.fixture(autouse=True)
def _clear_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear all config env vars before each test (env-independent).

    `Settings(_env_file=None)` disables the .env FILE but still reads `os.environ`, so a
    stray exported var would leak into the "defaults" assertions. Tests that exercise env
    overrides re-set the vars they need after this fixture runs.
    """
    for var in _CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Defaults (no env file, no env vars)
# ---------------------------------------------------------------------------


class TestDefaults:
    """Settings constructed with zero env must carry all devnet defaults."""

    def test_txline_base_url_default(self) -> None:
        """txline_base_url must default to the devnet endpoint."""
        s = Settings(_env_file=None)
        assert s.txline_base_url == "https://txline-dev.txodds.com/api"

    def test_solana_rpc_url_default(self) -> None:
        """solana_rpc_url must default to the Solana devnet RPC."""
        s = Settings(_env_file=None)
        assert s.solana_rpc_url == "https://api.devnet.solana.com"

    def test_solana_cluster_default(self) -> None:
        """solana_cluster must default to 'devnet'."""
        s = Settings(_env_file=None)
        assert s.solana_cluster == "devnet"

    def test_model_id_default(self) -> None:
        """model_id must default to the configured Claude model slug."""
        s = Settings(_env_file=None)
        assert s.model_id == "claude-sonnet-4-6"

    def test_decision_timeout_default(self) -> None:
        """decision_timeout_s must default to 30.0 seconds."""
        s = Settings(_env_file=None)
        assert s.decision_timeout_s == 30.0

    def test_secrets_are_none_by_default(self) -> None:
        """All secrets must be None when no env vars are present."""
        s = Settings(_env_file=None)
        assert s.txline_jwt is None
        assert s.txline_api_token is None
        assert s.solana_keypair_path is None
        assert s.database_url is None
        assert s.anthropic_api_key is None


# ---------------------------------------------------------------------------
# Env-var overrides
# ---------------------------------------------------------------------------


class TestEnvOverrides:
    """monkeypatched env vars must be resolved by Settings(_env_file=None)."""

    def test_jwt_env_var_maps_to_txline_jwt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JWT env var must map to txline_jwt field."""
        monkeypatch.setenv("JWT", "test-jwt-token")
        s = Settings(_env_file=None)
        assert s.txline_jwt == "test-jwt-token"

    def test_txline_x_api_token_maps_to_txline_api_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TXLINE_X_API_TOKEN env var must map to txline_api_token field."""
        monkeypatch.setenv("TXLINE_X_API_TOKEN", "test-api-token")
        s = Settings(_env_file=None)
        assert s.txline_api_token == "test-api-token"

    def test_database_url_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DATABASE_URL env var must map to database_url field."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/veridex")
        s = Settings(_env_file=None)
        assert s.database_url == "postgresql://user:pass@localhost/veridex"

    def test_anthropic_api_key_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ANTHROPIC_API_KEY env var must map to anthropic_api_key field."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        s = Settings(_env_file=None)
        assert s.anthropic_api_key == "sk-ant-test"

    def test_solana_keypair_path_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SOLANA_KEYPAIR_PATH env var must map to solana_keypair_path field."""
        monkeypatch.setenv("SOLANA_KEYPAIR_PATH", "/home/user/.config/solana/id.json")
        s = Settings(_env_file=None)
        assert s.solana_keypair_path == "/home/user/.config/solana/id.json"

    def test_multiple_env_vars_resolved_simultaneously(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Multiple env vars set at once must all be resolved correctly."""
        monkeypatch.setenv("JWT", "jwt-x")
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
        s = Settings(_env_file=None)
        assert s.txline_jwt == "jwt-x"
        assert s.database_url == "postgresql://localhost/test"


# ---------------------------------------------------------------------------
# require_txline
# ---------------------------------------------------------------------------


class TestRequireTxline:
    """require_txline must raise with a clear message or return (jwt, token)."""

    def test_raises_when_both_missing(self) -> None:
        """ValueError raised when both jwt and token are absent."""
        s = Settings(_env_file=None)
        with pytest.raises(ValueError, match="JWT and TXLINE_X_API_TOKEN"):
            require_txline(s)

    def test_raises_when_jwt_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ValueError raised when only api_token is configured."""
        monkeypatch.setenv("TXLINE_X_API_TOKEN", "token-only")
        s = Settings(_env_file=None)
        with pytest.raises(ValueError, match="TxLINE creds missing"):
            require_txline(s)

    def test_raises_when_token_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ValueError raised when only jwt is configured."""
        monkeypatch.setenv("JWT", "jwt-only")
        s = Settings(_env_file=None)
        with pytest.raises(ValueError, match="TxLINE creds missing"):
            require_txline(s)

    def test_returns_tuple_when_both_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns (jwt, api_token) tuple when both credentials are present."""
        monkeypatch.setenv("JWT", "my-jwt")
        monkeypatch.setenv("TXLINE_X_API_TOKEN", "my-token")
        s = Settings(_env_file=None)
        jwt, token = require_txline(s)
        assert jwt == "my-jwt"
        assert token == "my-token"


# ---------------------------------------------------------------------------
# require_database_url
# ---------------------------------------------------------------------------


class TestRequireDatabaseUrl:
    """require_database_url must raise when DATABASE_URL is absent."""

    def test_raises_when_missing(self) -> None:
        """ValueError raised when database_url is None."""
        s = Settings(_env_file=None)
        with pytest.raises(ValueError, match="DATABASE_URL"):
            require_database_url(s)

    def test_returns_url_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns the database URL string when DATABASE_URL is configured."""
        url = "postgresql://user:pass@localhost/veridex"
        monkeypatch.setenv("DATABASE_URL", url)
        s = Settings(_env_file=None)
        assert require_database_url(s) == url


# ---------------------------------------------------------------------------
# require_anthropic_key
# ---------------------------------------------------------------------------


class TestRequireAnthropicKey:
    """require_anthropic_key must raise when ANTHROPIC_API_KEY is absent."""

    def test_raises_when_missing(self) -> None:
        """ValueError raised when anthropic_api_key is None."""
        s = Settings(_env_file=None)
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            require_anthropic_key(s)

    def test_returns_key_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns the API key string when ANTHROPIC_API_KEY is configured."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
        s = Settings(_env_file=None)
        assert require_anthropic_key(s) == "sk-ant-real"


# ---------------------------------------------------------------------------
# require_keypair_path
# ---------------------------------------------------------------------------


class TestRequireKeypairPath:
    """require_keypair_path must raise when SOLANA_KEYPAIR_PATH is absent."""

    def test_raises_when_missing(self) -> None:
        """ValueError raised when solana_keypair_path is None."""
        s = Settings(_env_file=None)
        with pytest.raises(ValueError, match="SOLANA_KEYPAIR_PATH"):
            require_keypair_path(s)

    def test_returns_path_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns the keypair path string when SOLANA_KEYPAIR_PATH is configured."""
        path = "/home/user/.config/solana/id.json"
        monkeypatch.setenv("SOLANA_KEYPAIR_PATH", path)
        s = Settings(_env_file=None)
        assert require_keypair_path(s) == path


# ---------------------------------------------------------------------------
# get_settings — cached accessor
# ---------------------------------------------------------------------------


class TestGetSettings:
    """get_settings must return a Settings instance and honour the cache."""

    def test_returns_settings_instance(self) -> None:
        """get_settings() must return a Settings object."""
        s = get_settings()
        assert isinstance(s, Settings)

    def test_returns_same_cached_instance(self) -> None:
        """Repeated calls to get_settings() must return the identical object."""
        assert get_settings() is get_settings()
