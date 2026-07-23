'use client';
import { useEffect, useState } from 'react';
import { useParams } from 'next/navigation';
import { AgentProfileScreen } from '@/components/screens/AgentProfileScreen';
import { AgentOpsDrawer } from '@/components/ops/AgentOpsDrawer';
import { useAgentOps } from '@/components/ops/useAgentOps';
import { getAgentProfile } from '@/lib/api';
import type { AgentProfileRecord } from '@/lib/catalog';
import styles from './page.module.css';

// Quick honest enrichment · the profile is resolved by the self-gating getAgentProfile reader. Mock ⇒
// the labeled DEMO AGENT_PROFILES fixture (or an honest not-found for an unknown id). Off-mock ⇒ a REAL
// (leaner) profile assembled from data we already serve (the directional board + the public roster),
// degrading honestly for fields no endpoint exposes; not-found / any error ⇒ null → the honest
// "Agent profile unavailable." state, NEVER a fabricated fixture off-mock (T-2 fixture prohibition).
export default function AgentProfilePage() {
  const params = useParams<{ agentId: string }>();
  const ops = useAgentOps();
  const [profile, setProfile] = useState<AgentProfileRecord | null>(null);
  useEffect(() => {
    let alive = true;
    getAgentProfile(params.agentId)
      .then((p) => { if (alive) setProfile(p); })
      .catch(() => { if (alive) setProfile(null); });
    return () => { alive = false; };
  }, [params.agentId]);
  if (!profile) return <p className={styles.notFound}>Agent profile unavailable.</p>;
  return (
    <>
      <AgentProfileScreen profile={profile} onOpenRuntime={ops.open} />
      <AgentOpsDrawer state={ops} />
    </>
  );
}
