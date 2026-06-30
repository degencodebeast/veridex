"""veridex_agent — the standalone deployable Veridex agent (WD-3).

A thin package that runs ONE agent on TxLINE under the same law/policy/proof seal as the arena,
but OUTSIDE the competition container, producing an anchored, self-verified proof. The
decoupled-standalone-run core (:mod:`veridex_agent.run`) is the shared foundation; the CLI
(:mod:`veridex_agent.cli`) is a thin wrapper over it.
"""
