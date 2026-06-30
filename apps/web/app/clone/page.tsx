'use client';
import { Suspense } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { ClonePreviewScreen } from '@/components/screens/ClonePreviewScreen';
import { AGENT_PROFILES } from '@/lib/fixtures/catalog';

function CloneInner() {
  const router = useRouter();
  const params = useSearchParams();
  const source = AGENT_PROFILES[params.get('source') ?? 'value_clv'] ?? AGENT_PROFILES.value_clv;
  return <ClonePreviewScreen source={source} onCommit={() => router.push('/dashboard')} />;
}

// useSearchParams requires a Suspense boundary in Next 15 to avoid de-opting the route.
export default function ClonePreviewPage() {
  return (
    <Suspense>
      <CloneInner />
    </Suspense>
  );
}
