"""Policy-gated execution guardrails (Phase 2B).

A pure, sync, LLM-free, deny-by-default policy layer that sits DOWNSTREAM of the sealed
run and decides whether a law-approved action may execute. Lives on the audited trust
path: no LLM/venue/async imports allowed here.
"""
