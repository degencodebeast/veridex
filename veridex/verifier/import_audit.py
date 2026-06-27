"""Import-boundary audit (gate 2/7). Test-driven (T3).

AST-walks a package dir and raises if any forbidden LLM SDK import is present. Adapted from
`agent-rank/backend/tests/test_rebalance_cat_no_llm_imports.py` (the "Hamel-pattern" static audit).
"""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN = {"agno", "anthropic", "openai", "google.generativeai", "litellm"}


def _is_forbidden(module: str | None) -> bool:
    """True if `module` (or any dotted prefix of it) is a forbidden LLM SDK.

    Prefix-matching catches `agno.os` via `agno` and `google.generativeai.types` via
    `google.generativeai`, while leaving sibling namespaces like `google.cloud` alone.
    """
    if not module:
        return False
    parts = module.split(".")
    return any(".".join(parts[:i]) in FORBIDDEN for i in range(1, len(parts) + 1))


def assert_no_llm_imports(package_dir: str | Path) -> None:
    """Raise AssertionError if a forbidden LLM SDK is imported under `package_dir`.

    Accepts either a package directory (walked recursively) or a single ``*.py`` file, so a
    single-module trust-path file (e.g. ``veridex/scoring.py``) can be audited directly without
    sweeping its sibling modules.
    """
    package_dir = Path(package_dir)
    pyfiles = [package_dir] if package_dir.is_file() else sorted(package_dir.rglob("*.py"))
    for py in pyfiles:
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_forbidden(alias.name):
                        raise AssertionError(f"Forbidden LLM import '{alias.name}' in {py}")
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                # level == 0 → absolute import; relative (`from . import x`) is a local ref.
                if _is_forbidden(node.module):
                    raise AssertionError(f"Forbidden LLM import '{node.module}' in {py}")
                # `from google import generativeai` hides the SDK in alias.name, not module.
                for alias in node.names:
                    composed = f"{node.module}.{alias.name}" if node.module else alias.name
                    if _is_forbidden(composed):
                        raise AssertionError(f"Forbidden LLM import '{composed}' in {py}")
