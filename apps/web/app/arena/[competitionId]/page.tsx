import { CockpitScreen } from '@/components/screens/cockpit/CockpitScreen';
import { getCockpitState } from '@/lib/api';

export default async function CockpitPage({ params }: { params: Promise<{ competitionId: string }> }) {
  const { competitionId } = await params;
  const initial = await getCockpitState(competitionId);
  return <CockpitScreen competitionId={competitionId} initial={initial} />;
}
