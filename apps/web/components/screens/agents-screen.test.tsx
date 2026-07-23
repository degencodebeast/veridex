import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { AgentsScreen } from '@/components/screens/AgentsScreen';
import type { PublicAgentRow } from '@/lib/catalog';

const scored: PublicAgentRow = {
  public_agent_id: 'pa_value', display_name: 'Value CLV', owner_public_label: 'acme',
  origin: 'byoa', proof_state: 'reproducible', archetype: 'value_clv', mode: 'numeric',
  avg_clv_bps: 18.4, runs: 14, valid_pct: 95.0,
};

const unscored: PublicAgentRow = {
  public_agent_id: 'pa_fresh', display_name: 'Fresh Agent', owner_public_label: 'demo',
  origin: 'official', proof_state: 'unscored', archetype: 'baseline', mode: null,
  avg_clv_bps: null, runs: null, valid_pct: null,
};

describe('AgentsScreen — PublicAgentRow identity contract (E3)', () => {
  it('renders the OWNER (owner_public_label) and ORIGIN columns from the public identity', () => {
    render(<AgentsScreen agents={[scored]} />);
    const table = screen.getByRole('table');
    expect(within(table).getByText(/OWNER/i)).toBeInTheDocument();
    expect(within(table).getByText(/ORIGIN/i)).toBeInTheDocument();
    const row = screen.getByRole('link', { name: /Value CLV/i }).closest('tr') as HTMLElement;
    expect(within(row).getByText('acme')).toBeInTheDocument();
    expect(within(row).getByText('byoa')).toBeInTheDocument();
  });

  it('links each row by public_agent_id', () => {
    render(<AgentsScreen agents={[scored]} />);
    expect(screen.getByRole('link', { name: /Value CLV/i })).toHaveAttribute('href', '/agents/pa_value');
  });

  it('an unscored row renders "—" for perf and an unscored PROOF badge (never a fabricated number/proof)', () => {
    render(<AgentsScreen agents={[unscored]} />);
    const row = screen.getByRole('link', { name: /Fresh Agent/i }).closest('tr') as HTMLElement;
    // honest unscored proof badge
    expect(within(row).getByText(/unscored/i)).toBeInTheDocument();
    // avg CLV + runs render em-dash (unscored → "—", never a fabricated 0)
    const dashes = within(row).getAllByText('—');
    expect(dashes.length).toBeGreaterThanOrEqual(2);
  });
});
