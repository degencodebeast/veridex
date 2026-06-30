import type { LeaderboardRow, ProofMode } from '@/lib/contracts';

// SEC-005 / AC-005: rank is avg_clv_bps ONLY. Eligibility/confidence/low_sample are
// NEVER consulted here — they are display-derived and must not reorder the board.
export function rankByAvgClv(rows: LeaderboardRow[]): LeaderboardRow[] {
  return [...rows]
    .sort((a, b) => b.avg_clv_bps - a.avg_clv_bps)
    .map((r, i) => ({ ...r, rank: i + 1 }));
}

// REQ-006: eligible = proof_mode in {reproducible, verified}, independent of rank.
export function isEligible(proof_mode: ProofMode): boolean {
  return proof_mode === 'reproducible' || proof_mode === 'verified';
}

// REQ-006: numeric color is the sign of the value (never decoration).
export function numericClass(value: number): 'pos' | 'neg' | 'zero' {
  if (value > 0) return 'pos';
  if (value < 0) return 'neg';
  return 'zero';
}

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
