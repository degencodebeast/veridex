import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { WalletChip } from '@/components/layout/WalletChip';

const ADDR = '0x2eE447430b19016391A20369F0430846e18Fa177';
// shortHash(ADDR) → 0x2eE4…a177
const connected = (props: Partial<Parameters<typeof WalletChip>[0]> = {}) =>
  render(<WalletChip connected address={ADDR} {...props} />);
const chip = () => screen.getByRole('button', { name: /OP 0x2eE4/i });

describe('WalletChip (REQ-002, disclosure pattern — session-driven)', () => {
  describe('signed out', () => {
    it('shows a working Connect-Wallet control that fires onConnect (real Privy login)', async () => {
      const user = userEvent.setup();
      const onConnect = vi.fn();
      render(<WalletChip connected={false} onConnect={onConnect} />);
      const btn = screen.getByRole('button', { name: /connect wallet/i });
      expect(btn).toBeEnabled();
      await user.click(btn);
      expect(onConnect).toHaveBeenCalledTimes(1);
      // No fake operator chip when signed out.
      expect(screen.queryByText(/^OP /i)).toBeNull();
    });

    it('disables Connect-Wallet in an unconfigured build (no onConnect → no session possible)', () => {
      render(<WalletChip connected={false} />);
      expect(screen.getByRole('button', { name: /connect wallet/i })).toBeDisabled();
    });
  });

  it('renders nothing until Privy is ready (no connect-prompt flash at a persisted session)', () => {
    const { container } = render(<WalletChip ready={false} connected={false} onConnect={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  describe('signed in', () => {
    it('renders the REAL connected address (truncated), not a placeholder, with a Dashboard affordance', () => {
      connected();
      expect(chip()).toBeInTheDocument();
      expect(chip()).toHaveTextContent(/0x2eE4…a177/);
      expect(chip()).toHaveAccessibleName(/Dashboard/i);
      expect(screen.queryByText(/9xQe/)).toBeNull(); // the old hardcoded placeholder is gone
    });

    it('keeps the disclosure collapsed until clicked', () => {
      connected();
      expect(chip()).toHaveAttribute('aria-expanded', 'false');
      expect(screen.queryByRole('link', { name: 'Operator Dashboard' })).toBeNull();
    });

    it('opens a disclosure with account actions only — no prototype routes (judge-nav hygiene)', async () => {
      const user = userEvent.setup();
      connected();
      await user.click(chip());
      expect(chip()).toHaveAttribute('aria-expanded', 'true');
      expect(screen.getByRole('link', { name: 'Operator Dashboard' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument();
      // Prototype/incomplete routes are hidden from judge navigation.
      expect(screen.queryByRole('link', { name: 'Design System' })).toBeNull();
      expect(screen.queryByRole('link', { name: 'Clone Preview' })).toBeNull();
      expect(screen.queryByRole('link', { name: 'Prize Vault' })).toBeNull();
    });

    it('Disconnect fires onDisconnect (real Privy logout)', async () => {
      const user = userEvent.setup();
      const onDisconnect = vi.fn();
      connected({ onDisconnect });
      await user.click(chip());
      await user.click(screen.getByRole('button', { name: 'Disconnect' }));
      expect(onDisconnect).toHaveBeenCalledTimes(1);
    });

    it('closes on Escape (locks keydown listener cleanup)', async () => {
      const user = userEvent.setup();
      connected();
      await user.click(chip());
      expect(screen.getByRole('link', { name: 'Operator Dashboard' })).toBeInTheDocument();
      await user.keyboard('{Escape}');
      expect(screen.queryByRole('link', { name: 'Operator Dashboard' })).toBeNull();
      expect(chip()).toHaveAttribute('aria-expanded', 'false');
    });

    it('closes on outside mousedown (locks mousedown listener cleanup)', async () => {
      const user = userEvent.setup();
      render(
        <div>
          <button type="button">outside</button>
          <WalletChip connected address={ADDR} />
        </div>,
      );
      await user.click(chip());
      expect(screen.getByRole('link', { name: 'Operator Dashboard' })).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: 'outside' }));
      expect(screen.queryByRole('link', { name: 'Operator Dashboard' })).toBeNull();
      expect(chip()).toHaveAttribute('aria-expanded', 'false');
    });
  });
});
