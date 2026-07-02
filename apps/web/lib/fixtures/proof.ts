// The 6 REAL backend root-forest domains (veridex/chain/merkle.py build_root_forest) + labels.
// This is the single canonical order/label source the adapter and the demo both use — the names
// are backend truth, never invented. `mapRootForest` maps a served forest (A); PROOF_DEMO_ROOTS is
// the mock-only demo population (real names + demo hex) shown until the backend serializes root_forest.
import type { ProofRoot } from '@/lib/contracts';

export const PROOF_ROOT_ORDER = [
  'event_log', 'score', 'receipt', 'policy', 'competition', 'payout_reserved',
] as const;

export const PROOF_ROOT_LABELS: Record<string, string> = {
  event_log: 'Event Log',
  score: 'Score',
  receipt: 'Receipt',
  policy: 'Policy',
  competition: 'Competition',
  payout_reserved: 'Payout Reserved',
};

// Map a SERVED root_forest (domain → hex) into ordered, labeled ProofRoot[]. Absent ⇒ [] (honest).
export function mapRootForest(lineage: unknown): ProofRoot[] {
  const rf = (lineage as { root_forest?: Record<string, string> } | null)?.root_forest;
  if (!rf) return [];
  return PROOF_ROOT_ORDER.filter((d) => rf[d]).map((d) => ({ domain: d, label: PROOF_ROOT_LABELS[d], root: rf[d] }));
}

// sha256(b"") — the backend EMPTY_ROOT (veridex/chain/merkle.py:16) for a domain with no records.
export const EMPTY_ROOT = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';

// DEMO-only forest (mock). Real names, demo hex — never shown in a live view (honest-empty there).
// HONESTY: on the demo REPLAY path there is no executor lane, so receipt/policy/payout_reserved are
// genuinely EMPTY_ROOT (matches build_root_forest for empty domains) — demoed as empty, NOT
// fake-populated. Only the domains with real replay data carry demo hashes.
const DEMO_HEX: Record<string, string> = {
  event_log: '9f2c1a7b4e5d6038a1c2b3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718',
  score: 'b4e5d6038a1c2b3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f607189f2c1a7',
  competition: 'e5f60718293a4b5c6d7e8f90a1b2c3d4b4e5d6038a1c2b3d4e5f607189f2c1a7',
  receipt: EMPTY_ROOT,          // no executor lane on demo replay → honestly empty
  policy: EMPTY_ROOT,           // no executor lane on demo replay → honestly empty
  payout_reserved: EMPTY_ROOT,  // Phase 2D; never populated on Plan-A runs → honestly empty
};

export const PROOF_DEMO_ROOTS: ProofRoot[] = PROOF_ROOT_ORDER.map((d) => ({
  domain: d, label: PROOF_ROOT_LABELS[d], root: DEMO_HEX[d],
}));
