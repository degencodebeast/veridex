"""Strategy layer — Phase 2B Task 6.

Pure, sync, LLM-free proposal selection that reads ONLY the sealed run + deterministic law
output (``score_rows``). Strategies are downstream consumers of the seal; they NEVER recompute
edge/CLV (the law already did) and NEVER read an LLM-claimed edge or confidence.
"""
