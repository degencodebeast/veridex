'use client';
import { useEffect, useState } from 'react';
import { AgentsScreen } from '@/components/screens/AgentsScreen';
import { isMockEnabled } from '@/lib/mock';
import { AGENTS } from '@/lib/fixtures/catalog';
import type { AgentSummary } from '@/lib/catalog';

// T-2 remediation · /agents must NOT show a fabricated DIRECTIONAL roster with the demo flag OFF.
// There is NO agents-list backend reader/endpoint (lib/api.ts exposes none), so the directional roster
// is sourced CLIENT-side purely on the mock gate: isMockEnabled() reads the per-tab `?mock=1` from
// window (which a server render cannot see) — a judge toggling ?mock=1 gets the labeled DEMO fixture,
// while an off-mock judge gets an honest-empty roster ([]), NEVER the AGENTS fixture. This is the same
// mock-gated-fixture pattern as useAgentOps' RUNTIME_* demo path, not a fabricated static default.
export default function AgentsPage() {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  useEffect(() => {
    setAgents(isMockEnabled() ? AGENTS : []);
  }, []);
  return <AgentsScreen agents={agents} />;
}
