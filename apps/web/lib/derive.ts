import type { LeaderboardRow, ProofMode } from '@/lib/contracts';

// SEC-005 / AC-005: rank is avg_clv_bps ONLY. Eligibility/confidence/low_sample are
// NEVER consulted here — they are display-derived and must not reorder the board.
export function rankByAvgClv(rows: LeaderboardRow[]): LeaderboardRow[] {
  return [...rows]
    // null (unscored) sorts last — mirrors the backend's None → -inf ranking; identical order for
    // the always-numeric rows this helper actually receives (type-totality guard, not a logic change).
    .sort((a, b) => (b.avg_clv_bps ?? -Infinity) - (a.avg_clv_bps ?? -Infinity))
    .map((r, i) => ({ ...r, rank: i + 1 }));
}

// REQ-006: eligible = proof_mode in {reproducible, verified}, independent of rank.
export function isEligible(proof_mode: ProofMode): boolean {
  return proof_mode === 'reproducible' || proof_mode === 'verified';
}

// Numeric sign→color class lives in lib/format.ts as `signClass` (REQ-006) — reused here
// and by every numeric cell; no duplicate is kept in this module.

// WD-7 (REQ-054): CLV confidence from valid sample size; low-sample is flagged, never hidden.
export type ClvConfidence = 'high' | 'medium' | 'low';

export function clvConfidence(validCount: number): ClvConfidence {
  if (validCount >= 30) return 'high';
  if (validCount >= 10) return 'medium';
  return 'low';
}

export function isLowSample(validCount: number): boolean {
  return validCount < 10;
}
