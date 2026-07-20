'use client';
import { usePrivy } from '@privy-io/react-auth';
import { LandingScreen } from '@/components/screens/LandingScreen';

// The landing is PUBLIC marketing content — it always renders. Only the Connect-Wallet CTAs need a
// session, so (unlike the gated /dashboard + /competitions/create pages) we do NOT `return null` on
// `!ready`: that would blank the whole page. We just withhold `onConnect` until Privy is ready, and
// the screen hides the CTAs until then. usePrivy is read ONLY when Privy is configured
// (NEXT_PUBLIC_PRIVY_APP_ID) — mirroring AuthProvider's own guard, which mounts <PrivyProvider> only
// then; reading usePrivy outside that provider throws.
function SessionLanding() {
  const { ready, authenticated, user, login } = usePrivy();
  // Persisted Privy sessions rehydrate on load: once ready, reflect the real connected address in
  // the nav (→ Dashboard). Until ready, withhold onConnect so the CTA stays hidden (no dead button).
  return (
    <LandingScreen
      connected={ready && authenticated}
      address={user?.wallet?.address}
      onConnect={ready ? login : undefined}
    />
  );
}

export default function LandingPage() {
  const privyConfigured = Boolean(process.env.NEXT_PUBLIC_PRIVY_APP_ID);
  return privyConfigured ? <SessionLanding /> : <LandingScreen />;
}
