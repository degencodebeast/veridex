import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { CompetitionsScreen } from '@/components/screens/CompetitionsScreen';
import type { CompetitionSummary } from '@/lib/catalog';

// minimal builder for count/derivation teeth (real shape, no fabricated aggregate band)
function comp(id: string, lifecycle: CompetitionSummary['lifecycle'], over: Partial<CompetitionSummary> = {}): CompetitionSummary {
  return {
    competition_id: id, title: id, competition_type: 'replay_arena', lifecycle,
    source_mode: 'replay', execution_mode: 'paper', proof_mode: 'reproducible',
    market_scope: '1X2', roster_size: 2, events_per_min: null, ws_live: false,
    settled_run_id: lifecycle === 'settled' ? `${id}_run` : null, ...over,
  };
}

describe('CompetitionsScreen (REQ-014)', () => {
  it('routes a live competition to Enter Arena', () => {
    render(<CompetitionsScreen />);
    const live = screen.getByTestId('comp-wc-fra-bra');
    expect(within(live).getByRole('link', { name: /enter arena/i })).toHaveAttribute('href', '/arena/wc-fra-bra');
  });

  it('routes an upcoming competition to Join and a settled one to Proof', () => {
    render(<CompetitionsScreen />);
    // Join points at the competition's arena/detail page — NOT the create flow (joining ≠ creating).
    expect(within(screen.getByTestId('comp-wc-arg-ger')).getByRole('link', { name: /join/i }))
      .toHaveAttribute('href', '/arena/wc-arg-ger');
    expect(within(screen.getByTestId('comp-wc-esp-ned')).getByRole('link', { name: /proof/i }))
      .toHaveAttribute('href', '/proof/run_esp_ned_01');
  });

  it('shows the EVENTS/MIN + WS LIVE liveness tiles', () => {
    render(<CompetitionsScreen />);
    expect(screen.getAllByText(/EVENTS\/MIN/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/WS LIVE/i).length).toBeGreaterThanOrEqual(1);
  });

  it('renders a Recent Settled strip linking to a proof', () => {
    render(<CompetitionsScreen />);
    const strip = screen.getByTestId('recent-settled');
    expect(within(strip).getByRole('link', { name: /ESP v NED/i })).toHaveAttribute('href', '/proof/run_esp_ned_01');
  });

  it('is honest when empty — renders no fabricated competition rows (REQ-014 honest states)', () => {
    render(<CompetitionsScreen comps={[]} />);
    expect(screen.queryAllByTestId(/^comp-/)).toHaveLength(0);
    // the recent-settled strip exists but carries no fake proof links
    const strip = screen.getByTestId('recent-settled');
    expect(within(strip).queryAllByRole('link')).toHaveLength(0);
  });
});

// ── V5 fidelity: stat cards + ALL-COMPETITIONS table + prize/leader honesty ────
describe('CompetitionsScreen V5 (density · counts/prize/leader honesty)', () => {
  it('stat cards DERIVE counts from the real list — not a fabricated band', () => {
    const comps = [comp('a', 'live'), comp('b', 'live'), comp('c', 'upcoming'), comp('d', 'settled')];
    render(<CompetitionsScreen comps={comps} />);
    const stats = screen.getByTestId('stat-cards');
    expect(within(stats).getByTestId('stat-live')).toHaveTextContent('2');
    expect(within(stats).getByTestId('stat-upcoming')).toHaveTextContent('1');
    expect(within(stats).getByTestId('stat-settled')).toHaveTextContent('1');
    expect(within(stats).getByTestId('stat-total')).toHaveTextContent('4');
  });

  it('ALL-COMPETITIONS table carries TYPE/SOURCE/EXEC/PROOF/STATUS; a replay comp is REPLAY, never LIVE', () => {
    render(<CompetitionsScreen />);
    const table = screen.getByTestId('all-competitions');
    ['TYPE', 'SOURCE', 'EXEC', 'PROOF', 'STATUS'].forEach((h) =>
      expect(within(table).getAllByText(new RegExp(h, 'i')).length).toBeGreaterThanOrEqual(1));
    // the replay competition (arg-ger): SOURCE=replay, STATUS=upcoming — distinct axes, never LIVE.
    const row = within(table).getByTestId('comp-wc-arg-ger');
    expect(within(row).getByTestId('source-cell')).toHaveTextContent(/replay/i);
    expect(within(row).getByTestId('source-cell')).not.toHaveTextContent(/\blive\b/i);
    expect(within(row).getByTestId('status-cell')).toHaveTextContent(/upcoming/i);
  });

  it('the ALL-COMPETITIONS table preserves input order (no fabricated CLV-style ranking)', () => {
    render(<CompetitionsScreen />);
    const ids = within(screen.getByTestId('all-competitions'))
      .getAllByTestId(/^comp-/).map((r) => r.getAttribute('data-testid'));
    expect(ids).toEqual(['comp-wc-fra-bra', 'comp-wc-arg-ger', 'comp-wc-esp-ned']);
  });

  it('PRIZE is the honest design-target label (from rewards), never moved/paid funds', () => {
    render(<CompetitionsScreen />);
    const table = screen.getByTestId('all-competitions');
    // esp-ned has a reward entry → its honest amount_label ("— (design target)").
    expect(within(within(table).getByTestId('comp-wc-esp-ned')).getByTestId('prize-cell'))
      .toHaveTextContent(/design target/i);
    // never implies custody/payout happened anywhere.
    const prizes = within(table).getAllByTestId('prize-cell');
    expect(prizes.some((c) => /\$|paid|disbursed|funds (moved|settled)/i.test(c.textContent ?? ''))).toBe(false);
  });

  it('LEADER CLV is "—" everywhere — no fabricated comp leader (no honest comp→leader link)', () => {
    render(<CompetitionsScreen />);
    const cells = within(screen.getByTestId('all-competitions')).getAllByTestId('leader-cell');
    expect(cells.length).toBeGreaterThan(0);
    expect(cells.every((c) => c.textContent === '—')).toBe(true);
    expect(cells.some((c) => /\d/.test(c.textContent ?? ''))).toBe(false);
  });
});
