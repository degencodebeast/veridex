import { describe, it, expect, vi } from 'vitest';
import { render, screen, within, fireEvent } from '@testing-library/react';
import { ReplayLibrary } from '@/components/screens/ReplayLibrary';
import type { ReplayPackView } from '@/lib/api';

const PACK: ReplayPackView = {
  packId: 'curated',
  contentHash: 'deadbeefcafe',
  provenance: 'genuine-txline',
  isGenuine: true,
  fixtures: [18209181, 18213979],
  fixtureMetadata: [
    { fixture_id: 18209181, home_team: 'France', away_team: 'Morocco', kickoff_ts: 1783627200, label_source: 'captured' },
    { fixture_id: 18213979, home_team: 'Norway', away_team: 'England', kickoff_ts: 1783803600, label_source: 'captured' },
  ],
};

describe('ReplayLibrary', () => {
  it('renders pack identity + raw ids + captured labels + provenance + hash', () => {
    render(<ReplayLibrary packs={[PACK]} onLaunch={() => {}} />);
    expect(screen.getByText('curated')).toBeTruthy();
    expect(screen.getByText('genuine-txline')).toBeTruthy();
    expect(screen.getByText(/deadbeefcafe/)).toBeTruthy();
    expect(screen.getByText(/France v Morocco/)).toBeTruthy();
    expect(screen.getByText(/18209181/)).toBeTruthy();
  });

  it('shows honest empty state when no packs are admitted', () => {
    render(<ReplayLibrary packs={[]} onLaunch={() => {}} />);
    expect(screen.getByText(/No replay packs/i)).toBeTruthy();
  });

  it('degrades honestly for an unavailable label: raw id + "label unavailable", never fabricated', () => {
    const unlabeled: ReplayPackView = {
      ...PACK,
      fixtures: [18209181, 99999999],
      fixtureMetadata: [
        { fixture_id: 18209181, home_team: 'France', away_team: 'Morocco', kickoff_ts: 1783627200, label_source: 'captured' },
        { fixture_id: 99999999, home_team: null, away_team: null, kickoff_ts: null, label_source: 'unavailable' },
      ],
    };
    render(<ReplayLibrary packs={[unlabeled]} onLaunch={() => {}} />);
    const row = screen.getByTestId('replay-fixture-99999999');
    expect(within(row).getByText(/label unavailable/i)).toBeTruthy();
    expect(within(row).getByText(/99999999/)).toBeTruthy();
  });

  it('fires onLaunch with the raw pack_id + fixture_id (identity, not label text)', () => {
    const onLaunch = vi.fn();
    render(<ReplayLibrary packs={[PACK]} onLaunch={onLaunch} />);
    const row = screen.getByTestId('replay-fixture-18209181');
    fireEvent.click(within(row).getByRole('button', { name: /launch competition/i }));
    expect(onLaunch).toHaveBeenCalledWith('curated', 18209181);
  });
});
