import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { PrizeVaultScreen } from '@/components/screens/PrizeVaultScreen';
import { MY_REWARDS } from '@/lib/fixtures/catalog';

describe('PrizeVaultScreen (REQ-024 / SEC-008)', () => {
  it('shows the honest 2D-not-wired banner and never implies paid', () => {
    render(<PrizeVaultScreen />);
    expect(screen.getByText(/payout wiring lands in phase 2d/i)).toBeInTheDocument();
    // Off-mock there is nothing paid anywhere on the screen (honest-empty).
    expect(screen.queryByText(/^paid$/i)).toBeNull();
  });

  it('renders the root LABELS and proposal status', () => {
    render(<PrizeVaultScreen />);
    expect(screen.getByText(/score_root/i)).toBeInTheDocument();
    expect(screen.getByText(/payout_root/i)).toBeInTheDocument();
    expect(screen.getByText(/proposed · unsigned/i)).toBeInTheDocument();
  });

  // --- T-2 honesty: OFF-MOCK renders ABSENCE, never fabricated proof artifacts -----------------
  it('OFF-MOCK never renders fabricated Merkle root hex — shows honest not-anchored labels', () => {
    render(<PrizeVaultScreen />);
    // The fabricated-looking roots must NOT appear as if real.
    expect(screen.queryByText(/0xscore_8a31f2/i)).toBeNull();
    expect(screen.queryByText(/0xpayout_pending/i)).toBeNull();
    // Honest design-ahead / pending state instead.
    expect(screen.getByText(/not yet anchored/i)).toBeInTheDocument();
    expect(screen.getByText(/no settled payout/i)).toBeInTheDocument();
  });

  it('OFF-MOCK renders an honest-empty payout list — never the MY_REWARDS fixture', () => {
    render(<PrizeVaultScreen />);
    // No fixture rows leak off-mock, and the list container itself is absent (honest-empty).
    expect(screen.queryByTestId('payout-list')).toBeNull();
    expect(screen.queryByText(/world cup · esp v ned/i)).toBeNull();
    expect(screen.queryByText(/design target/i)).toBeNull();
    expect(screen.getByTestId('payout-empty')).toBeInTheDocument();
  });

  // --- Mock ON: labeled DEMO data is allowed (page injects it behind isMockEnabled) -------------
  it('DEMO (mock on) shows the injected payout fixtures with honest state badges', () => {
    render(<PrizeVaultScreen demo payouts={MY_REWARDS} />);
    const list = screen.getByTestId('payout-list');
    expect(within(list).getByText(/design target/i)).toBeInTheDocument();
    expect(within(list).getAllByText(/pending/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByTestId('payout-empty')).toBeNull();
  });

  it('DEMO (mock on) may show a demo root, but it is explicitly labeled as demo', () => {
    render(<PrizeVaultScreen demo payouts={MY_REWARDS} />);
    expect(screen.getByText(/0xscore_8a31f2/i)).toBeInTheDocument();
    // The demo root carries a visible demo marker so it can never read as a real anchored root.
    expect(screen.getAllByText(/^demo$/i).length).toBeGreaterThanOrEqual(1);
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
