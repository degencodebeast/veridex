'use client';
import { useParams } from 'next/navigation';
import { AgentProfileScreen } from '@/components/screens/AgentProfileScreen';
import { AgentOpsDrawer } from '@/components/ops/AgentOpsDrawer';
import { useAgentOps } from '@/components/ops/useAgentOps';
import { AGENT_PROFILES } from '@/lib/fixtures/catalog';

export default function AgentProfilePage() {
  const params = useParams<{ agentId: string }>();
  const ops = useAgentOps();
  const profile = AGENT_PROFILES[params.agentId];
  if (!profile) return <p style={{ padding: 16 }}>Agent not found.</p>;
  return (
    <>
      <AgentProfileScreen profile={profile} onOpenRuntime={ops.open} />
      <AgentOpsDrawer state={ops} />
    </>
  );
}
