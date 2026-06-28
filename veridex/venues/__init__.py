"""Venue adapter layer — async I/O shell for betting exchange integrations.

This package is the **async shell** layer (CON-010): it handles all network
I/O with external betting venues and is explicitly OUTSIDE the deterministic
trust path.  It must NEVER be imported by ``law``, ``scoring``, ``leaderboard``,
``verifier``, ``checks``, ``ingest``, or ``policy``.

Public surface
--------------
- :mod:`veridex.venues.base` — value types (Quote, Order, SubmitAck, etc.)
  and the :class:`~veridex.venues.base.VenueAdapter` Protocol.
- :mod:`veridex.venues.sx_bet` — :class:`~veridex.venues.sx_bet.FakeVenueAdapter`
  (offline-safe, deterministic) and :class:`~veridex.venues.sx_bet.SXBetAdapter`
  (config-gated live skeleton).
"""
