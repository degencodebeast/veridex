import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SessionWalletChip } from '@/components/layout/SessionWalletChip';

// usePrivy is mocked (a FAKE Privy state) — no real Privy SDK / network calls in this test.
const usePrivyMock = vi.fn();
vi.mock('@privy-io/react-auth', () => ({ usePrivy: () => usePrivyMock() }));

const ORIGINAL = process.env.NEXT_PUBLIC_PRIVY_APP_ID;
afterEach(() => {
  process.env.NEXT_PUBLIC_PRIVY_APP_ID = ORIGINAL;
  vi.clearAllMocks();
});

describe('SessionWalletChip (live session seam for the app chrome)', () => {
  describe('Privy unconfigured', () => {
    beforeEach(() => {
      delete process.env.NEXT_PUBLIC_PRIVY_APP_ID;
    });
    it('renders the signed-out chip WITHOUT reading usePrivy (which throws outside a provider)', () => {
      render(<SessionWalletChip />);
      expect(screen.getByRole('button', { name: /connect wallet/i })).toBeInTheDocument();
      expect(usePrivyMock).not.toHaveBeenCalled();
    });
  });

  describe('Privy configured', () => {
    beforeEach(() => {
      process.env.NEXT_PUBLIC_PRIVY_APP_ID = 'test-app';
    });

    it('signed in: renders the REAL connected address from the session', () => {
      usePrivyMock.mockReturnValue({
        ready: true,
        authenticated: true,
        user: { wallet: { address: '0x2eE447430b19016391A20369F0430846e18Fa177' } },
        login: vi.fn(),
        logout: vi.fn(),
      });
      render(<SessionWalletChip />);
      expect(screen.getByRole('button', { name: /OP 0x2eE4…a177/i })).toBeInTheDocument();
    });

    it('signed out: renders the Connect-Wallet control wired to the real login', () => {
      usePrivyMock.mockReturnValue({
        ready: true, authenticated: false, user: null, login: vi.fn(), logout: vi.fn(),
      });
      render(<SessionWalletChip />);
      expect(screen.getByRole('button', { name: /connect wallet/i })).toBeEnabled();
    });

    it('not ready: renders nothing (avoids flashing a connect prompt at a persisted session)', () => {
      usePrivyMock.mockReturnValue({
        ready: false, authenticated: false, user: null, login: vi.fn(), logout: vi.fn(),
      });
      const { container } = render(<SessionWalletChip />);
      expect(container).toBeEmptyDOMElement();
    });
  });
});
