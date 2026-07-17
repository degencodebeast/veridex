import { MakerProofCardScreen } from '@/components/screens/proof/MakerProofCardScreen';
import { getMakerArenaResult } from '@/lib/api';

// Deep-link only (CONTEXTUAL_ROUTES), reached from any maker row's PROOF → on the Leaderboard /
// Agents / Duel maker lane — not a top-level nav tab. The sealed MM-R1 result is a single arena
// run (one falsification, one proof_card), so `id` (the agent_id the row was reached from) is
// display context only — it does not change which result is shown (SEC-005: single maker source).
export default async function MakerProofCardPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const result = await getMakerArenaResult();
  return <MakerProofCardScreen result={result} agentId={id} />;
}
