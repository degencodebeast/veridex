// E1: Markets launch link must carry BOTH pack_id + fixture_id (was fixture-only), and a fixture's
// off-mock/pack-sourced status must render the honest "CAPTURED REPLAY" label — never "FINAL"/"FINISHED"
// (the old `finished: true` overclaimed a live settled result for what is really a sealed replay pack).
import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { MarketsScreen } from '@/components/screens/MarketsScreen';
import type { FixtureSummary } from '@/lib/catalog';

const FIXTURE: FixtureSummary = {
  fixture_id: 18213979,
  pack_id: 'demo_pack_real',
  competition: 'demo_pack_real',
  participant1: 'FRA',
  participant2: 'BRA',
  start_time: '',
  in_running: false,
};

describe('MarketsScreen launch link + fixture status (E1: pack_id + fixture_id, CAPTURED REPLAY)', () => {
  it('the launch link href carries pack_id + fixture_id for the selected fixture', () => {
    render(<MarketsScreen fixtures={[FIXTURE]} oddsByFixture={{}} feedHealth={null} leaderboard={[]} />);
    // fixtures[0] auto-selects on mount.
    const href = screen.getByTestId('launch-competition').getAttribute('href') ?? '';
    expect(href).toContain('pack_id=demo_pack_real');
    expect(href).toContain('fixture_id=18213979');
  });

  it('the fixture row renders "CAPTURED REPLAY", never "FINAL"', () => {
    render(<MarketsScreen fixtures={[FIXTURE]} oddsByFixture={{}} feedHealth={null} leaderboard={[]} />);
    const row = screen.getByTestId('fixture-18213979');
    expect(within(row).getByText(/captured replay/i)).toBeInTheDocument();
    expect(within(row).queryByText(/final/i)).toBeNull();
    expect(screen.queryByText(/finished/i)).toBeNull();
  });
});
