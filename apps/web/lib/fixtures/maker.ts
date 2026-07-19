// Maker Arena lane (MM-R1) fixture — the ONE data source for every maker surface
// (Leaderboard/Agents/Duel maker lanes + the Maker Proof Card). SEC-005: adapted through
// adaptMakerArenaResult only — never adaptLeaderboard / adaptProofArtifact. Computed once at
// module load (the sealed mock fixture is already static JSON — no async fetch needed), so
// screens can consume it exactly like the existing LEADERBOARD_ROWS / AGENTS static fixtures.
import { adaptMakerArenaResult } from '@/lib/api';
import { MOCK_FIXTURES } from '@/lib/mock';
import type { MakerArenaResultView } from '@/lib/contracts';

export const MAKER_ARENA_RESULT: MakerArenaResultView = adaptMakerArenaResult(MOCK_FIXTURES.makerArenaResult);

// Presentation-only role/caption labels (not part of the sealed result — display copy per the
// handoff: txline-fair-mm = candidate, naive-mm = control).
export const MAKER_AGENT_META: Record<string, { role: 'candidate' | 'control'; caption: string }> = {
  'txline-fair-mm': { role: 'candidate', caption: 'TxLINE-fair candidate' },
  'naive-mm': { role: 'control', caption: 'naive control' },
};
