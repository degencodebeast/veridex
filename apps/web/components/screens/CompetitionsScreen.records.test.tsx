import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { CompetitionsScreen } from '@/components/screens/CompetitionsScreen';
import type { CompetitionRecordView } from '@/lib/api';

const REC: CompetitionRecordView = {
  competitionId: 'c_1', status: 'running', title: 'NLD v MAR · 1X2',
  sourceMode: 'replay', executionMode: 'paper', rosterSize: 2, runId: null,
};

describe('CompetitionsScreen — real records (off-mock)', () => {
  it('renders real records with a MATCHING count and NO contradictory zero/empty mock surfaces', () => {
    render(<CompetitionsScreen records={[REC]} />);
    const section = screen.getByTestId('real-competitions');
    const link = within(section).getByRole('link', { name: /NLD v MAR/ });
    expect(link.getAttribute('href')).toBe('/arena/c_1');
    expect(within(section).getByTestId('record-status-c_1').textContent).toContain('running');
    // Coherent count: a page with 1 real record reads 1 — never a contradictory TOTAL 0.
    expect(within(section).getByTestId('real-total').textContent).toContain('1');
    expect(screen.queryByTestId('competitions-empty')).toBeNull();
    // The mock-derived summary band + all-competitions list are GONE off-mock, so no "TOTAL 0" /
    // "No competitions to show." can appear ALONGSIDE a real record (the finding's contradiction).
    expect(screen.queryByTestId('stat-total')).toBeNull();
    expect(screen.queryByTestId('all-competitions-empty')).toBeNull();
    expect(screen.queryByTestId('all-competitions')).toBeNull();
  });

  it('renders an honest empty state with Create + Markets CTAs when there are no records', () => {
    render(<CompetitionsScreen records={[]} />);
    const empty = screen.getByTestId('competitions-empty');
    expect(within(empty).getByRole('link', { name: /create/i }).getAttribute('href')).toBe('/competitions/create');
    expect(within(empty).getByRole('link', { name: /markets/i }).getAttribute('href')).toBe('/markets');
    // A truly empty off-mock page shows ONLY the honest empty state — not the mock stat band's TOTAL 0.
    expect(screen.queryByTestId('stat-total')).toBeNull();
  });

  it('does NOT render the real-records section (and DOES render the mock band) when records is undefined (mock path)', () => {
    render(<CompetitionsScreen comps={[]} rewards={[]} />);
    expect(screen.queryByTestId('real-competitions')).toBeNull();
    // Mock path is UNCHANGED — the derived stat band still renders.
    expect(screen.getByTestId('stat-total')).toBeTruthy();
  });
});
