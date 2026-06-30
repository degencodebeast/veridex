import { OperatorDashboardScreen } from '@/components/screens/OperatorDashboardScreen';

// Prototype: the operator session is simulated-authorized (mirrors the always-present
// WalletChip "OP" session). The screen fail-closes on `connected` (SEC-008); swap this to
// the live wallet/auth signal — and wire onOpenRuntime to the Task-14 drawer — when they land.
export default function OperatorDashboardPage() {
  return <OperatorDashboardScreen connected />;
}
