import { describe, it, expect } from 'vitest';
import { render, screen, within, fireEvent } from '@testing-library/react';
import { MarketsScreen } from '@/components/screens/MarketsScreen';
import type { FixtureSummary, OddsUpdate } from '@/lib/catalog';

// MAJOR-4 — two packs share the SAME external fixture_id but carry DIFFERENT odds. The screen must
// carry the composite (pack_id, fixture_id) identity through selection, odds lookup, React keys, and the
// launch href — never collapse to fixture_id alone (which cross-wires pack A's table/launch into pack B).

const FIXTURE_ID = 18213979;
const PACK_A = 'pack_alpha';
const PACK_B = 'pack_beta';

function fx(pack: string, p1: string): FixtureSummary {
  return {
    fixture_id: FIXTURE_ID, pack_id: pack, competition: pack,
    participant1: p1, participant2: 'Away', start_time: '', in_running: false,
  };
}

// Distinct 1X2 odds per pack so the rendered CONSENSUS cell reveals which pack's table is showing.
function odds(homeMilli: number): OddsUpdate[] {
  return [{
    fixture_id: FIXTURE_ID, message_id: 'm', ts: 1, in_running: false,
    market_family: '1X2_PARTICIPANT_RESULT', market_parameters: null,
    price_names: ['Home', 'Draw', 'Away'], prices: [homeMilli, 3500, 6000],
    pct: ['50.000', '28.571', '16.667'],
  }];
}

const ODDS_BY_FIXTURE: Record<string, OddsUpdate[]> = {
  [`${PACK_A}::${FIXTURE_ID}`]: odds(1500), // pack A → 1.500
  [`${PACK_B}::${FIXTURE_ID}`]: odds(2500), // pack B → 2.500
};

const FIXTURES = [fx(PACK_A, 'Alpha'), fx(PACK_B, 'Beta')];

function launchHref(): string {
  return screen.getByTestId('launch-competition').getAttribute('href') ?? '';
}

describe('MarketsScreen pack-scoped identity (MAJOR-4: (pack_id, fixture_id))', () => {
  it('shows pack A odds + pack A launch href for the default-selected pack A fixture', () => {
    render(<MarketsScreen fixtures={FIXTURES} oddsByFixture={ODDS_BY_FIXTURE} feedHealth={null} leaderboard={[]} />);
    const fam = screen.getByTestId('families');
    expect(within(fam).getByText('1.500')).toBeInTheDocument();  // pack A odds
    expect(within(fam).queryByText('2.500')).toBeNull();          // pack B odds must NOT leak in
    const href = launchHref();
    expect(href).toContain(`pack_id=${PACK_A}`);
    expect(href).toContain(`fixture_id=${FIXTURE_ID}`);
  });

  it('selecting pack B shows pack B odds + pack B launch href (no collision with pack A)', () => {
    const { container } = render(
      <MarketsScreen fixtures={FIXTURES} oddsByFixture={ODDS_BY_FIXTURE} feedHealth={null} leaderboard={[]} />,
    );
    const rowB = container.querySelector(`[data-fixture-key="${PACK_B}::${FIXTURE_ID}"]`);
    expect(rowB).not.toBeNull();
    fireEvent.click(rowB!);
    const fam = screen.getByTestId('families');
    expect(within(fam).getByText('2.500')).toBeInTheDocument();  // pack B odds
    expect(within(fam).queryByText('1.500')).toBeNull();          // pack A odds must NOT leak in
    expect(launchHref()).toContain(`pack_id=${PACK_B}`);
  });
});
