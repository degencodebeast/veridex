import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MarketsScreen } from '@/components/screens/MarketsScreen';
import type { OddsUpdate } from '@/lib/catalog';

// In-running-only odds (no pre-match capture) → closings cannot be reconstructed → pending/—
// (the honest CON-040 branch). Covers all three soccer families.
const IN_RUNNING: Record<number, OddsUpdate[]> = {
  18172280: [
    { fixture_id: 18172280, message_id: 'a', ts: 1, in_running: true, market_family: '1X2_PARTICIPANT_RESULT', market_parameters: null, price_names: ['FRA', 'Draw', 'BRA'], prices: [1472, 3550, 6100], pct: ['67.935', '28.169', '16.393'] },
    { fixture_id: 18172280, message_id: 'b', ts: 2, in_running: true, market_family: 'OVERUNDER_PARTICIPANT_GOALS', market_parameters: 'line=2.5', price_names: ['Over', 'Under'], prices: [1910, 1980], pct: ['52.356', '50.505'] },
    { fixture_id: 18172280, message_id: 'c', ts: 3, in_running: true, market_family: 'ASIANHANDICAP_PARTICIPANT_GOALS', market_parameters: 'line=-0.25', price_names: ['FRA', 'BRA'], prices: [1880, 2010], pct: ['53.191', '49.751'] },
  ],
};

describe('MarketsScreen (REQ-016 / AC-010/011 / REQ-042 / CON-040)', () => {
  it('marks Soccer active and US College FB/BB disabled with a "not in free feed" label (AC-011)', () => {
    render(<MarketsScreen />);
    expect(screen.getByRole('button', { name: /Soccer/ })).not.toBeDisabled();
    const cfb = screen.getByRole('button', { name: /US College Football/ });
    expect(cfb).toBeDisabled();
    expect(screen.getAllByText(/not in free feed/i).length).toBeGreaterThanOrEqual(1);
  });

  it('reads odds from /odds/updates, never /odds/snapshot (AC-010)', async () => {
    const user = userEvent.setup();
    render(<MarketsScreen />);
    await user.click(screen.getByTestId('fixture-18172280'));
    const panel = screen.getByTestId('families');
    expect(panel.getAttribute('data-odds-path')).toBe('/odds/updates/18172280');
    expect(panel.getAttribute('data-odds-path')).not.toContain('snapshot');
  });

  it('renders the three families with decimal odds + implied % and pending/— closings (REQ-042/CON-040)', async () => {
    const user = userEvent.setup();
    render(<MarketsScreen oddsByFixture={IN_RUNNING} />);
    await user.click(screen.getByTestId('fixture-18172280'));
    const fam = screen.getByTestId('families');
    expect(within(fam).getByText(/Match Result/i)).toBeInTheDocument();
    expect(within(fam).getByText(/Over \/ Under/i)).toBeInTheDocument();
    expect(within(fam).getByText(/Asian Handicap/i)).toBeInTheDocument();
    expect(within(fam).getByText('1.472')).toBeInTheDocument(); // decimal odds (decoded, 3dp)
    expect(within(fam).getByText(/67\.935/)).toBeInTheDocument(); // full-precision implied %
    // in-running with no pre-match → closing pending/— (CON-040)
    expect(within(fam).getAllByText(/pending|—/).length).toBeGreaterThanOrEqual(1);
  });

  it('does NOT render any unsupported TxLINE field — only the 4-field decimal outcome (#5 / REQ-042)', async () => {
    const user = userEvent.setup();
    render(<MarketsScreen oddsByFixture={IN_RUNNING} />);
    await user.click(screen.getByTestId('fixture-18172280'));
    const fam = screen.getByTestId('families');
    // No American odds / point-spread / depth-liquidity / per-bookmaker / possession-style stats.
    expect(within(fam).queryByText(/moneyline|american|spread|handicap line price|depth|liquidity|book(maker)?|possession|xg|corners/i)).toBeNull();
    // The visible column headers are exactly the supported decimal-odds set.
    expect(within(fam).getAllByText(/IMPLIED %/i).length).toBeGreaterThanOrEqual(1);
    expect(within(fam).getAllByText(/CLOSING/i).length).toBeGreaterThanOrEqual(1);
  });

  it('reconstructs a real closing value from pre-match updates (CON-040 value branch)', async () => {
    const user = userEvent.setup();
    // pre-match update (in_running:false) → closing reconstructable to a decimal value.
    const preMatch: Record<number, OddsUpdate[]> = {
      18172280: [
        { fixture_id: 18172280, message_id: 'p', ts: 1, in_running: false, market_family: '1X2_PARTICIPANT_RESULT', market_parameters: null, price_names: ['FRA', 'Draw', 'BRA'], prices: [1500, 3500, 6000], pct: ['66.667', '28.571', '16.667'] },
      ],
    };
    render(<MarketsScreen oddsByFixture={preMatch} />);
    await user.click(screen.getByTestId('fixture-18172280'));
    const fam = screen.getByTestId('families');
    expect(within(fam).getAllByText('1.500').length).toBeGreaterThanOrEqual(1); // decimal AND closing both 1.500
    expect(within(fam).queryAllByText(/pending|—/)).toHaveLength(0); // closing is a value, not pending
  });
});
