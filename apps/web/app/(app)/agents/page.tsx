'use client';
import { useEffect, useState } from 'react';
import { AgentsScreen } from '@/components/screens/AgentsScreen';
import { getAgentsRoster } from '@/lib/api';
import { isMockEnabled } from '@/lib/mock';
import { AGENTS } from '@/lib/fixtures/catalog';
import { agentSummaryToPublicRow } from '@/lib/agent-roster';
import type { PublicAgentRow } from '@/lib/catalog';

// The DIRECTIONAL roster is mock-gated. Mock ON (per-tab `?mock=1`, which a server render cannot see):
// the labeled DEMO AGENTS fixture. Mock OFF: the REAL public roster of deployed instances via
// getAgentsRoster() (GET /agents/roster) — honest performance columns ("—", never fabricated). Any
// fetch failure falls back to honest-empty ([]) — NEVER the AGENTS fixture off-mock (T-2 prohibition).
export default function AgentsPage() {
  const [agents, setAgents] = useState<PublicAgentRow[]>([]);
  useEffect(() => {
    let alive = true;
    if (isMockEnabled()) {
      // Map the demo AGENTS (AgentSummary[]) through the SHARED adapter → PublicAgentRow[].
      setAgents(AGENTS.map(agentSummaryToPublicRow));
      return;
    }
    getAgentsRoster()
      .then((roster) => { if (alive) setAgents(roster); })
      .catch(() => { if (alive) setAgents([]); }); // honest-empty on error — never a fabricated roster
    return () => { alive = false; };
  }, []);
  return <AgentsScreen agents={agents} />;
}
