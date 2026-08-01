'use client';
import { usePrivy } from '@privy-io/react-auth';
import { WalletChip } from './WalletChip';

// The live session seam for the app chrome. usePrivy() is read ONLY when Privy is configured
// (NEXT_PUBLIC_PRIVY_APP_ID) — mirroring AuthProvider's own guard, which mounts <PrivyProvider>
// only then; reading usePrivy outside that provider throws. In an unconfigured build the chip
// renders its signed-out state (no session is possible there anyway).
function LiveWalletChip() {
  const { ready, authenticated, user, login, logout } = usePrivy();
  return (
    <WalletChip
      ready={ready}
      connected={authenticated}
      address={user?.wallet?.address}
      onConnect={login}
      onDisconnect={logout}
    />
  );
}

export function SessionWalletChip() {
  const privyConfigured = Boolean(process.env.NEXT_PUBLIC_PRIVY_APP_ID);
  if (!privyConfigured) return <WalletChip connected={false} />;
  return <LiveWalletChip />;
}
