// auth-contract@1: "No token → UI shows login, NEVER fires an unauthenticated call." AuthGate is
// the reusable fail-closed wrapper a screen puts around owner-scoped affordances (e.g. the Studio
// deploy button — wiring that INTO Studio is a separate task; this only proves the gate itself).
//
// usePrivy is mocked (a FAKE Privy state) — no real Privy SDK / network calls in this test.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AuthGate } from '@/components/auth/AuthGate';

const usePrivyMock = vi.fn();
vi.mock('@privy-io/react-auth', () => ({
  usePrivy: () => usePrivyMock(),
}));

afterEach(() => {
  vi.clearAllMocks();
});

describe('AuthGate (auth-contract@1 fail-closed login gate)', () => {
  it('not ready: renders neither the login prompt nor children (no flash of the wrong state)', () => {
    usePrivyMock.mockReturnValue({ ready: false, authenticated: false, login: vi.fn() });
    render(
      <AuthGate>
        <button type="button" onClick={() => { throw new Error('must never fire'); }}>Deploy</button>
      </AuthGate>,
    );
    expect(screen.queryByRole('button', { name: /deploy/i })).not.toBeInTheDocument();
  });

  it('no session (unauthenticated): shows a login affordance and NEVER renders the gated action', async () => {
    const deploy = vi.fn();
    const login = vi.fn();
    usePrivyMock.mockReturnValue({ ready: true, authenticated: false, login });
    render(
      <AuthGate>
        <button type="button" onClick={deploy}>Deploy</button>
      </AuthGate>,
    );
    // The gated action is not in the DOM at all — it structurally cannot fire unauthenticated.
    expect(screen.queryByRole('button', { name: /deploy/i })).not.toBeInTheDocument();
    expect(deploy).not.toHaveBeenCalled();
    // A login affordance is shown instead.
    const loginButton = screen.getByRole('button', { name: /log ?in/i });
    const user = userEvent.setup();
    await user.click(loginButton);
    expect(login).toHaveBeenCalledTimes(1);
  });

  it('authenticated: renders the gated children (deploy affordance is reachable)', () => {
    const deploy = vi.fn();
    usePrivyMock.mockReturnValue({ ready: true, authenticated: true, login: vi.fn() });
    render(
      <AuthGate>
        <button type="button" onClick={deploy}>Deploy</button>
      </AuthGate>,
    );
    expect(screen.getByRole('button', { name: /deploy/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /log ?in/i })).not.toBeInTheDocument();
  });
});
