"""Pure-tier market-maker strategy package (MM-R4-B).

This package is the DETERMINISTIC, side-effect-free strategy tier: given an observation,
a prior state, and a frozen config, it decides a quote/hold with no network, no I/O, no
wall clock, and no randomness. Its purity is a load-bearing trust boundary — the pure
modules ``{contracts, config, basis, core}`` import ONLY stdlib + pydantic +
``veridex.mm_strategy.contracts`` + ``veridex.runtime.evidence`` (the shared canonical
serializer). Nothing here reaches ``veridex.dust_execution`` / ``veridex.live_recorder`` /
``veridex.venues`` / ``veridex.maker`` / ``veridex.scoring`` / an LLM SDK.

The purity guard ``tests/test_mm_strategy_purity.py`` enforces that whitelist both by an
AST scan and by a fresh-subprocess import audit (import-time AND post-``decide()``), so this
``__init__`` deliberately performs NO eager adapter/venue import.
"""
