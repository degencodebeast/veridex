// The edge DISPLAY GATE (REQ-2D-501 / AC-2D-501) — the presentation-layer honesty line.
//
// An executable-edge number (and the venue price / mispricing gap derived from it) renders ONLY
// when a REAL venue quote backs it. This generalizes the Inspector's original null-guard: the gate
// keys on the real-quote SIGNAL, never merely "a number is non-null". A Fake/paper quote (the
// FakeVenueAdapter's fixed 2.05 decimal price) is NOT a real venue quote — so its numbers must
// never be presented as edge. Fail-closed: any missing piece ⇒ no edge.

export interface EdgeQuoteSignal {
  venue_decimal_price: number | null;
  executable_edge_bps: number | null;
  real_venue_quote: boolean;
}

/** True iff a REAL venue quote backs both a venue price and an executable edge (fail-closed). */
export function hasRealVenueQuote(s: EdgeQuoteSignal): boolean {
  return s.real_venue_quote === true && s.venue_decimal_price != null && s.executable_edge_bps != null;
}
