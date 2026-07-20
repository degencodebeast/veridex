"""Unit tests for the Postgres DSN scheme normalization (veridex.api.server._normalize_pg_dsn).

``psycopg``/``psycopg_pool`` speak libpq and only understand ``postgresql://`` / ``postgres://``. A
SQLAlchemy-style ``postgresql+psycopg://…`` DSN makes libpq raise ``missing "=" after …`` and the pool
never opens (the exact failure that broke the Docker stack on compose-recreate). The pool factory
normalizes the scheme so BOTH the plain and ``+driver`` forms work, instead of relying on every
operator remembering to strip ``+psycopg``. These tests lock that behavior in.

Importing ``veridex.api.server`` is side-effect free (it never builds the app), so this stays offline.
"""

from __future__ import annotations

import pytest

from veridex.api.server import _normalize_pg_dsn


@pytest.mark.parametrize(
    "raw,expected",
    [
        # SQLAlchemy +driver forms are rewritten to the plain libpq scheme.
        ("postgresql+psycopg://u:p@h:5432/db", "postgresql://u:p@h:5432/db"),
        ("postgresql+psycopg2://u:p@h:5432/db", "postgresql://u:p@h:5432/db"),
        ("postgresql+asyncpg://u:p@h/db", "postgresql://u:p@h/db"),
        ("postgres+psycopg://u:p@h/db", "postgres://u:p@h/db"),
        # Already-plain DSNs pass through UNCHANGED (both scheme spellings).
        ("postgresql://u:p@h:5432/db", "postgresql://u:p@h:5432/db"),
        ("postgres://u:p@h/db", "postgres://u:p@h/db"),
        # A libpq key=value conninfo (not a URL) is left untouched.
        ("host=h port=5432 dbname=db user=u", "host=h port=5432 dbname=db user=u"),
        # Scheme match is case-insensitive.
        ("POSTGRESQL+PSYCOPG://u@h/db", "POSTGRESQL://u@h/db"),
    ],
)
def test_normalize_pg_dsn(raw: str, expected: str) -> None:
    assert _normalize_pg_dsn(raw) == expected


def test_normalize_pg_dsn_only_rewrites_the_scheme_prefix() -> None:
    """Anchored at ``^``: a ``+psycopg://`` appearing later (e.g. in a password) is NOT rewritten."""
    dsn = "postgresql://user:pa+psycopg://ss@h/db"
    assert _normalize_pg_dsn(dsn) == dsn


def test_normalize_pg_dsn_is_idempotent() -> None:
    """Normalizing an already-normalized DSN is a no-op (safe to apply repeatedly)."""
    once = _normalize_pg_dsn("postgresql+psycopg://u:p@h:5432/db")
    assert _normalize_pg_dsn(once) == once == "postgresql://u:p@h:5432/db"
