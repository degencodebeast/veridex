import { GuardAblationScreen } from '@/components/screens/proof/GuardAblationScreen';

// Deep-link only — reached from the OWNER-SCOPED deployed-instance page (`/instances/{id}`), the sole
// valid origin: the ablation fetches the owner-scoped GET /maker/live-ab/{instanceId} keyed by
// instance_id, so it lives in the INSTANCE identity domain. The back-link therefore returns to that
// instance page — NEVER to /proof/maker/{id}, which is the PUBLIC historical (agent_id-keyed) card and
// would render unrelated MM-R1 evidence under this instance id (cross-domain identity leak).
export default async function GuardAblationPage({ params }: { params: Promise<{ instanceId: string }> }) {
  const { instanceId } = await params;
  return <GuardAblationScreen instanceId={instanceId} backHref={`/instances/${instanceId}`} />;
}
