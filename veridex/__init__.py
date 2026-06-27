"""Veridex — TxLINE Agent Proof Arena.

LLM agents compete on live or replayed TxLINE sports-betting markets. A deterministic
"law" recomputes the math (edge / CLV / Kelly), scores agents by closing-line value, and
anchors tamper-evident proof records on Solana. The LLM proposes; the deterministic law
disposes — the LLM never self-certifies.

The trust path (``checks/``, ``verifier/``, ``law/``, ``ingest/`` + ``scoring.py`` +
``leaderboard.py``) imports no LLM SDK; the LLM decision shell lives in ``runtime/agent.py``
(via OpenRouter), outside that boundary. The separation is enforced by a static import audit.
"""
