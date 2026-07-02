import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { OperatorDashboardScreen } from '@/components/screens/OperatorDashboardScreen';
import { MY_AGENTS } from '@/lib/fixtures/catalog';

describe('OperatorDashboardScreen (REQ-012 / SEC-008)', () => {
  it('renders the personal sections and the primary buttons (connected)', () => {
    render(<OperatorDashboardScreen connected />);
    expect(screen.getByRole('heading', { name: /your agents/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /your runs/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /your rewards/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /new agent/i })).toHaveAttribute('href', '/studio');
    expect(screen.getByRole('link', { name: /join competition/i })).toHaveAttribute('href', '/competitions');
  });

  it('does NOT leak operator-private data when disconnected — fail-closed, ABSENT from DOM (#6 / SEC-008)', () => {
    render(<OperatorDashboardScreen connected={false} />);
    // Private sections are not in the DOM at all (not merely visually hidden).
    expect(screen.queryByRole('heading', { name: /your agents/i })).toBeNull();
    expect(screen.queryByRole('heading', { name: /your runs/i })).toBeNull();
    expect(screen.queryByTestId('your-rewards')).toBeNull();
    expect(screen.queryByTestId('alerts-rail')).toBeNull();
    // No private agent identity leaks.
    MY_AGENTS.forEach((a) => expect(screen.queryByText(a.agent_name)).toBeNull());
    // Honest connect prompt instead.
    expect(screen.getByText(/connect.*wallet/i)).toBeInTheDocument();
  });

  it('renders private operator data only once connected (the gate flips it on)', () => {
    render(<OperatorDashboardScreen connected />);
    expect(screen.getByRole('heading', { name: /your agents/i })).toBeInTheDocument();
    // (name can appear in both Your Agents and Your Runs — presence, not uniqueness)
    expect(screen.getAllByText(MY_AGENTS[0].agent_name).length).toBeGreaterThanOrEqual(1);
  });

  it('opens the runtime drawer from a Your-Agents row (REQ-012 -> REQ-030)', async () => {
    const user = userEvent.setup();
    const onOpenRuntime = vi.fn();
    render(<OperatorDashboardScreen connected onOpenRuntime={onOpenRuntime} />);
    await user.click(screen.getAllByRole('button', { name: /runtime/i })[0]);
    expect(onOpenRuntime).toHaveBeenCalled();
  });

  it('labels rewards with honest payout states, never implying paid (SEC-008)', () => {
    render(<OperatorDashboardScreen connected />);
    const rewards = screen.getByTestId('your-rewards');
    expect(within(rewards).getByText(/design target/i)).toBeInTheDocument();
    expect(within(rewards).getAllByText(/pending/i).length).toBeGreaterThanOrEqual(1);
    expect(within(rewards).queryByText(/^paid$/i)).toBeNull();
  });

  it('shows a FAILED payout as a distinct negative state, never a misleading pending badge (honesty)', () => {
    render(
      <OperatorDashboardScreen
        connected
        rewards={[{ competition_id: 'x', title: 'X Cup', amount_label: '— (failed)', payout_state: 'failed' }]}
      />,
    );
    const rewards = screen.getByTestId('your-rewards');
    const failed = within(rewards).getByText('failed');
    expect(failed).toHaveAttribute('data-payout', 'failed'); // distinct negative span, not a Badge
    expect(within(rewards).queryByText(/pending/i)).toBeNull(); // a failure is NOT shown as pending
  });

  it('shows the alerts rail with kill/deny/hold items', () => {
    render(<OperatorDashboardScreen connected />);
    const rail = screen.getByTestId('alerts-rail');
    expect(within(rail).getByText('DENY')).toBeInTheDocument();
    expect(within(rail).getByText('HOLD')).toBeInTheDocument();
  });
});
