import { ProofCardScreen } from '@/components/screens/proof/ProofCardScreen';
import { getProofArtifact } from '@/lib/api';

export default async function ProofCardPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  const artifact = await getProofArtifact(runId);
  return <ProofCardScreen artifact={artifact} />;
}
