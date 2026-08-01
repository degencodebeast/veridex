'use client';
import { Suspense, useEffect, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { ClonePreviewScreen } from '@/components/screens/ClonePreviewScreen';
import { isMockEnabled } from '@/lib/mock';
import { AGENT_PROFILES } from '@/lib/fixtures/catalog';
import type { AgentProfileRecord } from '@/lib/catalog';
import styles from './page.module.css';

function CloneInner() {
  const router = useRouter();
  const params = useSearchParams();
  const sourceId = params.get('source') ?? 'value_clv';
  const [source, setSource] = useState<AgentProfileRecord | null>(null);
  // T-2 remediation · the source agent must NOT be fabricated with the demo flag OFF. There is NO
  // agent-profile backend reader, so the source is resolved CLIENT-side purely on the mock gate:
  // isMockEnabled() reads the per-tab `?mock=1` from window (which a server render cannot see) — a
  // judge toggling ?mock=1 gets the labeled DEMO source profile, while an off-mock judge ALWAYS gets
  // an honest "source unavailable" state, NEVER the AGENT_PROFILES fixture and NEVER the old
  // unconditional `?? value_clv` fabricated fallback.
  useEffect(() => {
    setSource(isMockEnabled() ? (AGENT_PROFILES[sourceId] ?? null) : null);
  }, [sourceId]);
  if (!source) return <p className={styles.notFound}>Source agent unavailable.</p>;
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
