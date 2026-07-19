// F-3: the Operator Dashboard's `connected` gate is DERIVED from the real auth session
// (usePrivy().authenticated), not a hardcoded literal. usePrivy is only read when Privy is
// configured (NEXT_PUBLIC_PRIVY_APP_ID) — mirroring AuthProvider's own guard — so an
// unconfigured build fail-closes rather than crashing outside <PrivyProvider>.
//
// usePrivy is mocked (FAKE Privy state); getInstances is stubbed so the connected render never
// touches the network.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';

const usePrivyMock = vi.fn();
vi.mock('@privy-io/react-auth', () => ({ usePrivy: () => usePrivyMock() }));
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, getInstances: vi.fn(async () => []) };
});

import OperatorDashboardPage from './page';

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllEnvs();
});

describe('OperatorDashboardPage — connected derived from the real session (not hardcoded)', () => {
  it('authenticated session: renders the private dashboard (Your Agents)', () => {
    vi.stubEnv('NEXT_PUBLIC_PRIVY_APP_ID', 'app-123');
    usePrivyMock.mockReturnValue({ ready: true, authenticated: true, login: vi.fn(), getAccessToken: vi.fn() });
    render(<OperatorDashboardPage />);
    expect(screen.getByRole('heading', { name: /your agents/i })).toBeInTheDocument();
  });

  it('Privy NOT ready: renders nothing until initialized (no connect-gate flash to an authed operator)', () => {
    vi.stubEnv('NEXT_PUBLIC_PRIVY_APP_ID', 'app-123');
    usePrivyMock.mockReturnValue({ ready: false, authenticated: false, login: vi.fn(), getAccessToken: vi.fn() });
    render(<OperatorDashboardPage />);
    expect(screen.queryByText(/connect.*wallet/i)).toBeNull();
    expect(screen.queryByRole('heading', { name: /your agents/i })).toBeNull();
  });

  it('unauthenticated session: fail-closes to the connect prompt (no private data)', () => {
    vi.stubEnv('NEXT_PUBLIC_PRIVY_APP_ID', 'app-123');
    const login = vi.fn();
    usePrivyMock.mockReturnValue({ ready: true, authenticated: false, login, getAccessToken: vi.fn() });
    render(<OperatorDashboardPage />);
    expect(screen.queryByRole('heading', { name: /your agents/i })).toBeNull();
    // The connect gate is shown, and its login control is wired to the real Privy login.
    const gate = screen.getByTestId('connect-gate');
    expect(gate).toBeInTheDocument();
    within(gate).getByRole('button', { name: /connect wallet/i }).click();
    expect(login).toHaveBeenCalledTimes(1);
  });

  it('Privy not configured: fail-closes (no session possible) without reading usePrivy', () => {
    // No NEXT_PUBLIC_PRIVY_APP_ID → the page must NOT call usePrivy (would throw outside a provider).
    render(<OperatorDashboardPage />);
    expect(usePrivyMock).not.toHaveBeenCalled();
    expect(screen.getByText(/connect.*wallet/i)).toBeInTheDocument();
  });
});
