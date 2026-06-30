import { InspectorScreen } from '@/components/screens/inspector/InspectorScreen';
import { getInspectorRecord } from '@/lib/api';

export default async function InspectorPage({ params }: { params: Promise<{ runId: string; seq: string }> }) {
  const { runId, seq } = await params;
  const record = await getInspectorRecord(runId, seq);
  return <InspectorScreen record={record} />;
}
