"""SEC-005 import isolation for the R1.5/R2 maker modules.

These modules form a SEALED maker lane: they MUST NOT import the directional
scorer (``veridex.scoring``), reference its rank internals
(``score_run``/``_rank_key``), or import any LLM SDK. The scan is static
(AST + source) so it is order-independent and cannot be defeated by lazy
imports done inside functions.
"""

import ast
import importlib
import inspect

# The R1.5/R2 modules added/extended by this extension. Each must stay isolated
# from the directional scorer and free of any LLM SDK.
NEW_R15_R2_MODULES = (
    "veridex.maker.trade_artifact",
    "veridex.maker.capture",
    "veridex.maker.r2_suite",
    "veridex.maker.diagnostic",
    "veridex.maker.runner",
)

# LLM SDK roots the sealed maker lane must never import.
BANNED_LLM_ROOTS = {
    "openai",
    "anthropic",
    "litellm",
    "agno",
    "cohere",
    "mistralai",
    "ollama",
    "google",  # google.generativeai
    "groq",
    "vertexai",
}

# Directional-scorer symbols/modules the sealed maker lane must never touch.
FORBIDDEN_SOURCE_TOKENS = ("veridex.scoring", "score_run", "_rank_key")


def _module_source(modname: str) -> str:
    return inspect.getsource(importlib.import_module(modname))


def _import_roots(modname: str) -> set[str]:
    tree = ast.parse(_module_source(modname))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _scoring_imports(modname: str) -> set[str]:
    """Fully-qualified ``veridex.scoring`` imports reachable in this module."""
    tree = ast.parse(_module_source(modname))
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits |= {a.name for a in node.names if a.name.startswith("veridex.scoring")}
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "veridex.scoring" or node.module.startswith(
                "veridex.scoring."
            ):
                hits.add(node.module)
    return hits


def test_new_r15_r2_modules_have_no_directional_or_llm_import():
    for modname in NEW_R15_R2_MODULES:
        roots = _import_roots(modname)
        banned = roots & BANNED_LLM_ROOTS
        assert not banned, f"{modname} imports a banned LLM SDK: {banned}"

        scoring = _scoring_imports(modname)
        assert not scoring, f"{modname} imports the directional scorer: {scoring}"

        source = _module_source(modname)
        for token in FORBIDDEN_SOURCE_TOKENS:
            assert token not in source, (
                f"{modname} references forbidden directional-scorer token "
                f"{token!r} (maker lane must stay sealed)"
            )
