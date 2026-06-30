import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { PrizeVaultScreen } from '@/components/screens/PrizeVaultScreen';

describe('PrizeVaultScreen (REQ-024 / SEC-008)', () => {
  it('shows the honest 2D-not-wired banner and never implies paid', () => {
    render(<PrizeVaultScreen />);
    expect(screen.getByText(/payout wiring lands in phase 2d/i)).toBeInTheDocument();
    const list = screen.getByTestId('payout-list');
    expect(within(list).queryByText(/^paid$/i)).toBeNull();
  });

  it('renders the Squads roots and proposal status', () => {
    render(<PrizeVaultScreen />);
    expect(screen.getByText(/score_root/i)).toBeInTheDocument();
    expect(screen.getByText(/payout_root/i)).toBeInTheDocument();
  });

  it('labels each payout with an honest state badge', () => {
    render(<PrizeVaultScreen />);
    const list = screen.getByTestId('payout-list');
    expect(within(list).getByText(/design target/i)).toBeInTheDocument();
    expect(within(list).getAllByText(/pending/i).length).toBeGreaterThanOrEqual(1);
  });

  it('shows a FAILED payout as a distinct negative state, never a misleading pending badge (honesty)', () => {
    render(
      <PrizeVaultScreen
        payouts={[{ competition_id: 'x', title: 'X Cup', amount_label: '— (failed)', payout_state: 'failed' }]}
      />,
    );
    const failed = within(screen.getByTestId('payout-list')).getByText('failed');
    expect(failed).toHaveAttribute('data-payout', 'failed');
  });

  it('exposes no wired Connect/Sign payout action (read-only designed surface)', () => {
    render(<PrizeVaultScreen />);
    expect(screen.queryByRole('button', { name: /sign payout|send funds|pay out/i })).toBeNull();
  });
});
