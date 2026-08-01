import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { MarketsScreen } from '@/components/screens/MarketsScreen';
import { getReplayMarkets } from '@/lib/api';
import type { FixtureSummary, OddsUpdate } from '@/lib/catalog';

// E2 / MAJOR-5 — the EXACT unit conversion from the wire replay-market projection to the rendered
// odds table. A loose test ("a number appears") would let odds render wrong, so every string is pinned.

afterEach(() => { vi.unstubAllGlobals(); });

const PACK_ID = 'demo_pack_real';
const FIXTURE_ID = 18213979;

// Wire ReplayMarketsResponse: two OVERUNDER markets (two param rows) —
//   • line=2.5 NON-suspended: stable_price 2.08 / stable_prob_bps 5000 → 2.080 + 50.000%
//   • line=3.5 SUSPENDED:    stable_price 1.93 / stable_prob_bps {}   → 1.930 + — (no fabricated %)
const WIRE = {
  fixture_id: FIXTURE_ID,
  label: 'CAPTURED REPLAY',
  markets: [
    {
      market_key: 'OVERUNDER_PARTICIPANT_GOALS||line=2.5',
      in_running: false,
      suspended: false,
      ts: 100,
      stable_prob_bps: { p1: 5000 },
      stable_price: { p1: 2.08 },
    },
    {
      market_key: 'OVERUNDER_PARTICIPANT_GOALS||line=3.5',
      in_running: false,
      suspended: true,
      ts: 100,
      stable_prob_bps: {},
      stable_price: { p1: 1.93 },
    },
  ],
};

function stubFetch(body: unknown, status = 200) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(body), { status })) as unknown as typeof fetch);
}

function fixture(): FixtureSummary {
  return {
    fixture_id: FIXTURE_ID,
    pack_id: PACK_ID,
    competition: PACK_ID,
    participant1: 'Home',
    participant2: 'Away',
    start_time: '',
    in_running: false,
  };
}

// Render the screen off the REAL reader output (default-selects the only fixture → table populates).
async function renderFromWire(): Promise<HTMLElement> {
  stubFetch(WIRE);
  const updates: OddsUpdate[] = await getReplayMarkets(PACK_ID, FIXTURE_ID);
  // Odds are keyed by the composite (pack_id, fixture_id) identity (MAJOR-4).
  render(<MarketsScreen oddsByFixture={{ [`${PACK_ID}::${FIXTURE_ID}`]: updates }} fixtures={[fixture()]} feedHealth={null} leaderboard={[]} />);
  return screen.getByTestId('families');
}

// The six-column odds row: [selection, consensus(decimal), implied %, closing, edge, agents]. The
// CONSENSUS cell (index 1) is the disambiguator; the CLOSING cell (index 3) MUST render an honest
// em-dash for a replay projection — the source DTO deliberately OMITS closing, so no reconstructed or
// cross-parameter number may appear there.
function rowCells(fam: HTMLElement, consensusDecimal: string): HTMLElement[] {
  const rows = within(fam).getAllByRole('row').map((tr) => within(tr).queryAllByRole('cell'));
  const match = rows.find((cells) => cells.length === 6 && cells[1].textContent === consensusDecimal);
  if (!match) throw new Error(`no data row whose CONSENSUS cell is ${consensusDecimal}`);
  return match;
}

describe('MarketsScreen replay-market odds render (E2 / MAJOR-5 exact conversion)', () => {
  it('a non-suspended market renders decimal 2.080 and implied 50.000% (EXACT)', async () => {
    const fam = await renderFromWire();
    const cells = rowCells(fam, '2.080');
    expect(cells[1].textContent).toBe('2.080');   // decimal odds: 2.08 → ×1000 → decodePrice → 2.080
    expect(cells[2].textContent).toBe('50.000%'); // implied %: 5000 bps → 50.000%
  });

  it('a suspended market renders price 1.930 and implied — (no fabricated %)', async () => {
    const fam = await renderFromWire();
    const cells = rowCells(fam, '1.930');
    expect(cells[1].textContent).toBe('1.930');       // last-known odds retained
    expect(cells[2].textContent).toBe('—');           // EMPTY prob → honest em-dash
    expect(/\d/.test(cells[2].textContent ?? '')).toBe(false); // never a fabricated "0.000"
  });

  it('EVERY replay CLOSING cell renders EXACTLY — (absent, not "pending" — a hash-bound replay closing never arrives)', async () => {
    const fam = await renderFromWire();
    // Both same-family O/U parameter lines (2.5 → 2.080, 3.5 → 1.930) must independently render the
    // PLAIN em-dash — proving no reconstruction, no cross-parameter borrowing, AND no "pending / —"
    // overclaim: for a hash-bound CAPTURED REPLAY the closing is genuinely ABSENT and never forthcoming.
    for (const consensus of ['2.080', '1.930']) {
      const cells = rowCells(fam, consensus);
      expect(cells[3].textContent).toBe('—');                      // EXACT plain em-dash — not "pending / —", not a number
      expect(/\d/.test(cells[3].textContent ?? '')).toBe(false);   // never a reconstructed/borrowed price
    }
  });

  it('a LIVE null closing MAY still render "pending / —" (a live in-play closing genuinely is forthcoming)', async () => {
    // In-play (in_running:true) → no pre-match update exists, so reconstructClosing yields null. Under a
    // LIVE source that null is honestly "pending / —" (a closing prints once the pre-match window closes),
    // NOT the plain replay em-dash — the live path keeps the forthcoming label.
    stubFetch({
      fixture_id: FIXTURE_ID,
      label: 'LIVE',
      markets: [{
        market_key: 'OVERUNDER_PARTICIPANT_GOALS||line=2.5',
        in_running: true, suspended: false, ts: 100,
        stable_prob_bps: { p1: 5000 }, stable_price: { p1: 2.08 },
      }],
    });
    const updates = await getReplayMarkets(PACK_ID, FIXTURE_ID);
    render(<MarketsScreen oddsByFixture={{ [`${PACK_ID}::${FIXTURE_ID}`]: updates }} fixtures={[fixture()]} sourceMode="live" feedHealth={null} leaderboard={[]} />);
    const fam = screen.getByTestId('families');
    const cells = rowCells(fam, '2.080');
    expect(cells[3].textContent).toBe('pending / —');              // live null closing keeps the forthcoming label
  });

  it('the EDGE column renders an honest — (no fabricated executable edge)', async () => {
    const fam = await renderFromWire();
    const edge = within(fam).getAllByTestId('edge-cell');
    expect(edge.length).toBeGreaterThan(0);
    expect(edge.every((c) => c.textContent === '—')).toBe(true);
  });

  it('a market_key whose params segment contains a pipe keeps the FULL params (no truncation)', async () => {
    // market_key = {SuperOddsType}|{MarketPeriod}|{MarketParameters}; a pipe inside MarketParameters
    // must be preserved (index-2-onward), not silently dropped at the first split boundary.
    stubFetch({
      fixture_id: FIXTURE_ID,
      label: 'CAPTURED REPLAY',
      markets: [
        {
          market_key: 'OVERUNDER_PARTICIPANT_GOALS|FULLTIME|line=2.5|side=over',
          in_running: false,
          suspended: false,
          ts: 100,
          stable_prob_bps: { p1: 5000 },
          stable_price: { p1: 2.08 },
        },
      ],
    });
    const [update] = await getReplayMarkets(PACK_ID, FIXTURE_ID);
    expect(update.market_family).toBe('OVERUNDER_PARTICIPANT_GOALS');
    expect(update.market_parameters).toBe('line=2.5|side=over');
  });
});
