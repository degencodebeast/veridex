import { describe, it, expect } from 'vitest';
import { hasRealVenueQuote } from '@/lib/edge-gate';

// The presentation-layer honesty line (REQ-2D-501 / AC-2D-501). An executable-edge number renders
// ONLY when a REAL venue quote backs it. Gate on the real-quote SIGNAL, never merely "a number
// exists" — a Fake/paper quote (FakeVenueAdapter's fixed 2.05) is NOT a real venue quote.
describe('edge display gate (hasRealVenueQuote)', () => {
  it('is FALSE when there is no venue price (honest-absence — nothing to render)', () => {
    expect(hasRealVenueQuote({ venue_decimal_price: null, executable_edge_bps: null, real_venue_quote: false })).toBe(false);
  });

  it('is FALSE for a Fake/paper quote even though a price+number exist (the FakeVenueAdapter 2.05 case)', () => {
    // A number is present (price 2.05, edge 512) but it is NOT from a real venue → never edge.
    expect(hasRealVenueQuote({ venue_decimal_price: 2.05, executable_edge_bps: 512, real_venue_quote: false })).toBe(false);
  });

  it('is FALSE when the real-quote flag is set but the price/edge are missing (fail-closed)', () => {
    expect(hasRealVenueQuote({ venue_decimal_price: null, executable_edge_bps: 22, real_venue_quote: true })).toBe(false);
    expect(hasRealVenueQuote({ venue_decimal_price: 1.472, executable_edge_bps: null, real_venue_quote: true })).toBe(false);
  });

  it('is TRUE only for a real venue quote with both a price and an edge', () => {
    expect(hasRealVenueQuote({ venue_decimal_price: 1.472, executable_edge_bps: 22, real_venue_quote: true })).toBe(true);
  });
});
