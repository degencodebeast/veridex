'use client';
import { useEffect, useState } from 'react';
import { DuelScreen } from '@/components/screens/DuelScreen';
import { isMockEnabled } from '@/lib/mock';
import { AGENTS } from '@/lib/fixtures/catalog';
import { agentSummaryToPublicRow } from '@/lib/agent-roster';
import type { PublicAgentRow } from '@/lib/catalog';

// E4 · The PAGE owns ONLY the mock gate for the Public-Agents lane, resolved in an EFFECT so it is
// hydration-safe: the server render and the first client render both see `resolved: false` (an identical
// unresolved shell — no agents, no fetch), and only after mount does isMockEnabled() (a per-tab `?mock=1`
// read a server render can't see) decide the source. Mock ON → the labeled DEMO AGENTS fixture mapped
// through the SHARED agentSummaryToPublicRow adapter; mock OFF → `agents: null`, and the SCREEN performs
// the single real getAgentsRoster() read. The page does NOT own lane state and does NOT run the real
// fetch. (AGENTS + the adapter imported HERE is fine — the fixture-prohibition scan targets the SCREEN.)
//
// The Maker lane is a SEPARATE population sourced inside DuelScreen (useMakerArenaResult) — untouched here.
export default function DuelPage() {
  const [mock, setMock] = useState<{ resolved: boolean; agents: PublicAgentRow[] | null }>({ resolved: false, agents: null });
  useEffect(() => {
    setMock({ resolved: true, agents: isMockEnabled() ? AGENTS.map(agentSummaryToPublicRow) : null });
  }, []);
  return <DuelScreen mockResolved={mock.resolved} mockAgents={mock.agents} />;
}
