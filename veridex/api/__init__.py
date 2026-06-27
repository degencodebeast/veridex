"""Veridex FastAPI demo surface — B11b (REQ-115 / AC-115).

Gated behind the ``api`` optional extra (``fastapi>=0.115``). The deterministic core
(``veridex.runtime.competition``, ``veridex.scoring``, etc.) MUST NOT import from this
package so the trust path stays FastAPI-free (CON-007 / CON-010).

Import path::

    from veridex.api.router import create_app

No auth / Redis / rate-limiting in Phase 1 (CON-009).
"""
