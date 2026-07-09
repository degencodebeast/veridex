"""Veridex live-recorder lane (MM-R3): isolated event-contract module.

Standalone pydantic v2 contracts for the live-recorder event stream. This package
deliberately imports nothing from ``veridex.chain.merkle``, ``veridex.scoring``,
``veridex.maker``, or any guard surface — it is a pure contract layer.
"""
