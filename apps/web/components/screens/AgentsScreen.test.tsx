import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AgentsScreen } from '@/components/screens/AgentsScreen';

describe('AgentsScreen (REQ-017)', () => {
  it('links Compare Two -> Duel and Create Agent -> Studio', () => {
    render(<AgentsScreen />);
    expect(screen.getByRole('link', { name: /compare two/i })).toHaveAttribute('href', '/duel');
    expect(screen.getByRole('link', { name: /create agent/i })).toHaveAttribute('href', '/studio');
  });

  it('links each agent row to its profile', () => {
    render(<AgentsScreen />);
    expect(screen.getByRole('link', { name: /Value CLV/i })).toHaveAttribute('href', '/agents/value_clv');
  });

  it('filters by search text', async () => {
    const user = userEvent.setup();
    render(<AgentsScreen />);
    await user.type(screen.getByRole('searchbox'), 'momentum');
    expect(screen.getByRole('link', { name: /Momentum FR/i })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Value CLV/i })).toBeNull();
  });

  it('is honest when empty — renders no fabricated agent rows (REQ-017 honest states)', () => {
    render(<AgentsScreen agents={[]} />);
    const agentLinks = screen.queryAllByRole('link').filter((l) => l.getAttribute('href')?.startsWith('/agents/'));
    expect(agentLinks).toHaveLength(0);
    expect(screen.getByTestId('agents-empty')).toBeInTheDocument();
  });
});
