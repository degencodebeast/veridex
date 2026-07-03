import { CockpitScreen } from '@/components/screens/cockpit/CockpitScreen';
import { getCockpitState, getFeedHealth } from '@/lib/api';

export default async function CockpitPage({ params }: { params: Promise<{ competitionId: string }> }) {
  const { competitionId } = await params;
  const [initial, initialFeedHealth] = await Promise.all([getCockpitState(competitionId), getFeedHealth()]);
  return <CockpitScreen competitionId={competitionId} initial={initial} initialFeedHealth={initialFeedHealth} />;
}
