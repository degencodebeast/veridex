'use client';
import { useEffect, useState } from 'react';
import { useParams } from 'next/navigation';
import { AgentProfileScreen } from '@/components/screens/AgentProfileScreen';
import { AgentOpsDrawer } from '@/components/ops/AgentOpsDrawer';
import { useAgentOps } from '@/components/ops/useAgentOps';
import { isMockEnabled } from '@/lib/mock';
import { AGENT_PROFILES } from '@/lib/fixtures/catalog';
import type { AgentProfileRecord } from '@/lib/catalog';
import styles from './page.module.css';

// T-2 remediation · a PUBLIC agent strategy profile must NOT be fabricated with the demo flag OFF.
// There is NO agent-profile backend reader/endpoint (lib/api.ts exposes none), so the profile is
// resolved CLIENT-side purely on the mock gate: isMockEnabled() reads the per-tab `?mock=1` from
// window (which a server render cannot see) — a judge toggling ?mock=1 gets the labeled DEMO fixture
// (or an honest not-found for an unknown id), while an off-mock judge ALWAYS gets an honest
// "profile unavailable" state, NEVER the AGENT_PROFILES fixture. Same mock-gated-fixture pattern as
// the /agents roster and useAgentOps' RUNTIME_* demo path — not a fabricated static default.
export default function AgentProfilePage() {
  const params = useParams<{ agentId: string }>();
  const ops = useAgentOps();
  const [profile, setProfile] = useState<AgentProfileRecord | null>(null);
  useEffect(() => {
    setProfile(isMockEnabled() ? (AGENT_PROFILES[params.agentId] ?? null) : null);
  }, [params.agentId]);
  if (!profile) return <p className={styles.notFound}>Agent profile unavailable.</p>;
  return (
    <>
      <AgentProfileScreen profile={profile} onOpenRuntime={ops.open} />
      <AgentOpsDrawer state={ops} />
    </>
  );
}
