import ast, importlib, inspect, pkgutil
import veridex.maker as maker_pkg

BANNED = {"agno", "anthropic", "openai", "litellm"}


def _module_import_roots(modname: str) -> set[str]:
    # scan THIS module's own import statements (static, order-independent)
    tree = ast.parse(inspect.getsource(importlib.import_module(modname)))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_no_llm_sdk_imported_by_any_maker_module():
    for mod in pkgutil.iter_modules(maker_pkg.__path__, prefix="veridex.maker."):
        roots = _module_import_roots(mod.name)
        assert BANNED.isdisjoint(roots), f"{mod.name} imports a banned LLM SDK: {roots & BANNED}"


def _module_scoring_imports(modname: str) -> set[str]:
    # SEC-005: no maker module may import the directional scorer (sealed lane)
    tree = ast.parse(inspect.getsource(importlib.import_module(modname)))
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits |= {a.name for a in node.names if a.name.startswith("veridex.scoring")}
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "veridex.scoring" or node.module.startswith("veridex.scoring."):
                hits.add(node.module)
    return hits


def test_no_directional_scorer_imported_by_any_maker_module():
    # SEC-005: the maker lane stays sealed from veridex.scoring.score_run/_rank_key
    for mod in pkgutil.iter_modules(maker_pkg.__path__, prefix="veridex.maker."):
        scoring = _module_scoring_imports(mod.name)
        assert not scoring, f"{mod.name} imports the directional scorer: {scoring}"
