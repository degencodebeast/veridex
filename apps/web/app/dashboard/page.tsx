'use client';
import { useAgentOps } from '@/components/ops/useAgentOps';
import { OperatorDashboardScreen } from '@/components/screens/OperatorDashboardScreen';
import { AgentOpsDrawer } from '@/components/ops/AgentOpsDrawer';

// Prototype: the operator session is simulated-authorized (mirrors the always-present
// WalletChip "OP" session). The screen fail-closes on `connected` (SEC-008); swap this to
// the live wallet/auth signal when it lands. onOpenRuntime opens the read-only Ops drawer.
export default function OperatorDashboardPage() {
  const ops = useAgentOps();
  return (
    <>
      <OperatorDashboardScreen connected onOpenRuntime={ops.open} />
      <AgentOpsDrawer state={ops} />
    </>
  );
}
