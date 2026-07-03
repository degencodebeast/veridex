"""Deploy layer (Phase-2D T21) — the Studio deploy preflight + pinned-instance model.

A submitted Studio config becomes a PINNED AgentInstance only AFTER a fail-closed, NAMED
preflight passes (:func:`veridex.deploy.preflight.run_deploy_preflight`). The launch itself
runs through the SINGLE runner seam (``veridex_agent.run.standalone_run``) — never a parallel
deploy path. This package holds only pure value objects + the offline preflight; the FastAPI
route wiring lives in :mod:`veridex.api.deploy`.
"""
