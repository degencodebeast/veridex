'use client';
import { usePrivy } from '@privy-io/react-auth';
import { useAgentOps } from '@/components/ops/useAgentOps';
import { OperatorDashboardScreen } from '@/components/screens/OperatorDashboardScreen';
import { AgentOpsDrawer } from '@/components/ops/AgentOpsDrawer';
import type { AgentOpsState } from '@/components/ops/useAgentOps';

// `connected` is DERIVED from the real auth session (auth-contract@1), never a hardcoded literal:
// the screen fail-closes (SEC-008) until the operator is authenticated. usePrivy is only read when
// Privy is CONFIGURED (NEXT_PUBLIC_PRIVY_APP_ID) — mirroring AuthProvider's own guard, which mounts
// <PrivyProvider> only then. Reading usePrivy outside that provider throws, so an unconfigured
// build fail-closes to the connect prompt instead (no session is possible there anyway).
//
// Before Privy initializes (`!ready`) we render nothing rather than flash the connect gate to an
// operator who is in fact already authenticated (same posture as components/auth/AuthGate). Once
// ready, the connect gate's login control is wired to the real Privy `login` via onConnect.
function SessionDashboard({ ops }: { ops: AgentOpsState }) {
  const { ready, authenticated, login } = usePrivy();
  if (!ready) return null;
  return <OperatorDashboardScreen connected={authenticated} onOpenRuntime={ops.open} onConnect={login} />;
}

export default function OperatorDashboardPage() {
  const ops = useAgentOps();
  const privyConfigured = Boolean(process.env.NEXT_PUBLIC_PRIVY_APP_ID);
  return (
    <>
      {privyConfigured
        ? <SessionDashboard ops={ops} />
        : <OperatorDashboardScreen connected={false} onOpenRuntime={ops.open} />}
      <AgentOpsDrawer state={ops} />
    </>
  );
}
