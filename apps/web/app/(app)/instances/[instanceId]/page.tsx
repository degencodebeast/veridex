'use client';
import { useParams } from 'next/navigation';
import { InstanceScreen } from '@/components/screens/InstanceScreen';

// Owner-scoped deployed-instance route. Client component: the instance is fetched in the browser so
// the auth-contract@1 bearer (wired into the client seam by AuthProvider) is attached — a
// server-render would carry no token and fail closed. InstanceScreen owns the loading/empty/error/
// success states; a 403/404 renders an honest not-found/unauthorized state, never a fabricated one.
export default function InstancePage() {
  const params = useParams<{ instanceId: string }>();
  return <InstanceScreen instanceId={params.instanceId} />;
}
