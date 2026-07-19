import { GuardAblationScreen } from '@/components/screens/proof/GuardAblationScreen';

// Deep-link only (mirrors the /proof/maker/[id] pattern) — reached from the Maker Proof Card's
// "QuoteGuard behavior ablation → OPEN" entry point, never a top-level nav tab. The screen fetches
// GET /maker/live-ab/{instanceId} client-side so it can render loading + retry states honestly; the
// back-link returns to the Maker Proof Card for the same identity.
export default async function GuardAblationPage({ params }: { params: Promise<{ instanceId: string }> }) {
  const { instanceId } = await params;
  return <GuardAblationScreen instanceId={instanceId} backHref={`/proof/maker/${instanceId}`} />;
}
