'use client';
import { useEffect, type ReactNode } from 'react';
import { PrivyProvider, usePrivy } from '@privy-io/react-auth';
import { setAuthTokenProvider } from '@/lib/auth';

// Wires Privy's getAccessToken() into the api client's injectable auth seam (lib/auth.ts). A
// separate inner component is required: usePrivy() only works INSIDE <PrivyProvider>.
function TokenSeamWiring() {
  const { getAccessToken } = usePrivy();
  useEffect(() => {
    setAuthTokenProvider(() => getAccessToken());
  }, [getAccessToken]);
  return null;
}

/**
 * App-wide Privy context (auth-contract@1 token acquisition). Wrap the root layout with this so
 * `usePrivy()` / the wired token seam are available everywhere — including screens that gate
 * owner-scoped actions behind {@link AuthGate}. This component itself gates NOTHING: public
 * screens (leaderboard, proof cards, cockpit) stay reachable without a session; only the seam
 * and `usePrivy()` context are provided here.
 *
 * No `NEXT_PUBLIC_PRIVY_APP_ID` configured (e.g. local/CI without secrets) → render children
 * directly, no PrivyProvider mount. The seam then stays at its fail-closed default (no provider
 * wired ⇒ getAuthToken() resolves null), so owner-scoped calls fire WITHOUT an Authorization
 * header rather than fabricating one — only the login UI/session wiring is skipped. The actual
 * "never fires unauthenticated" guarantee comes from wrapping the owner-scoped action in
 * {@link AuthGate} plus the backend's 401-before-any-side-effect boundary (auth-contract@1).
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  // auth-contract@1: env NEXT_PUBLIC_PRIVY_APP_ID (frontend). See .env.example. Read per-render
  // (not module-scope) so it reflects the actual runtime env, including in tests.
  const appId = process.env.NEXT_PUBLIC_PRIVY_APP_ID ?? '';
  if (!appId) return <>{children}</>;
  return (
    <PrivyProvider appId={appId} config={{ loginMethods: ['email', 'wallet'] }}>
      <TokenSeamWiring />
      {children}
    </PrivyProvider>
  );
}
