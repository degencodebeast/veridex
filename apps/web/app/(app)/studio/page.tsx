'use client';
import { useRouter } from 'next/navigation';
import { StudioScreen } from '@/components/screens/StudioScreen';

export default function AgentStudioPage() {
  const router = useRouter();
  // Navigate ONLY on a resolved, successful deploy — to the REAL instance page keyed by the
  // server-returned instance_id (F-3's route). StudioScreen awaits the deploy and calls this with
  // the result; on a fail-closed preflight it never fires, so a failed deploy STAYS on Studio.
  return <StudioScreen onPin={(result) => router.push(`/instances/${result.instance_id}`)} />;
}
