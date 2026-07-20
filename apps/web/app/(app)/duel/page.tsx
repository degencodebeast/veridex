'use client';
import { useEffect, useState } from 'react';
import { DuelScreen } from '@/components/screens/DuelScreen';
import { isMockEnabled } from '@/lib/mock';
import { AGENTS } from '@/lib/fixtures/catalog';
import type { AgentSummary } from '@/lib/catalog';

// T-2 remediation · /duel must NOT show a fabricated DIRECTIONAL head-to-head with the demo flag OFF.
// There is NO agents/duel backend reader/endpoint (lib/api.ts exposes none), so the directional agents are
// sourced CLIENT-side purely on the mock gate: isMockEnabled() reads the per-tab `?mock=1` from window
// (which a server render cannot see) — a judge toggling ?mock=1 gets the labeled DEMO fixture, while an
// off-mock judge gets an honest-empty directional duel ([] → the "select two agents" empty state), NEVER
// the AGENTS fixture. Mirrors the sibling /agents fix; not a fabricated static default.
//
// The Maker lane (F-9) is a SEPARATE population sourced from its own fixture inside DuelScreen — untouched
// here; this gate is strictly the directional agents prop.
export default function DuelPage() {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  useEffect(() => {
    setAgents(isMockEnabled() ? AGENTS : []);
  }, []);
  return <DuelScreen agents={agents} />;
}
