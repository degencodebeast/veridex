// Supplementary coverage for the thin Privy-glue wiring (the 3 REQUIRED auth-contract@1 RED
// tests live in lib/api.auth.test.ts and components/auth/AuthGate.test.tsx and were written
// test-first). @privy-io/react-auth is mocked — no real SDK/network calls.
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AuthProvider } from '@/components/auth/AuthProvider';
import { getAuthToken, resetAuthTokenProvider } from '@/lib/auth';

const getAccessTokenMock = vi.fn(async () => 'privy-token-xyz');
vi.mock('@privy-io/react-auth', () => ({
  PrivyProvider: ({ children }: { children: React.ReactNode }) => <div data-testid="privy-provider">{children}</div>,
  usePrivy: () => ({ getAccessToken: getAccessTokenMock }),
}));

beforeEach(() => {
  vi.stubEnv('NEXT_PUBLIC_PRIVY_APP_ID', 'test-app-id');
});
afterEach(() => {
  vi.unstubAllEnvs();
  resetAuthTokenProvider();
});

describe('AuthProvider (auth-contract@1: wires Privy getAccessToken into the api-client seam)', () => {
  it('mounts PrivyProvider and wires the seam so getAuthToken() delegates to Privy getAccessToken()', async () => {
    render(<AuthProvider><p>app</p></AuthProvider>);
    expect(screen.getByTestId('privy-provider')).toBeInTheDocument();
    expect(await getAuthToken()).toBe('privy-token-xyz');
    expect(getAccessTokenMock).toHaveBeenCalled();
  });

  it('never gates content — public screens stay reachable without a session', () => {
    render(<AuthProvider><p>public content</p></AuthProvider>);
    expect(screen.getByText('public content')).toBeInTheDocument();
  });

  it('no NEXT_PUBLIC_PRIVY_APP_ID configured: renders children directly, no PrivyProvider mount', () => {
    vi.unstubAllEnvs(); // undo the beforeEach stub for this one case
    render(<AuthProvider><p>unconfigured</p></AuthProvider>);
    expect(screen.getByText('unconfigured')).toBeInTheDocument();
    expect(screen.queryByTestId('privy-provider')).not.toBeInTheDocument();
  });
});
