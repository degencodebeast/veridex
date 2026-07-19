import { describe, it, expect } from 'vitest';
import {
  deriveCounterfactualCapacity,
  MAKER_EVIDENCE_CLASSES,
} from '@/lib/makerVerdict';
import type { MakerCapacityClaim } from '@/lib/contracts';

// AC-30 / AC-31 (Fable M3) — the maker honesty review layer. Gate-B REQ-026/027/052/053 doctrine:
// a third-party print / counterfactual capacity is BOUNDED by matched observed liquidity and LABELED
// counterfactual — it can NEVER render as our own fill / receipt / PnL / rank / Gate-B authority.
// A weaker evidence class may never up-cast into a stronger one.
describe('maker evidence classes stay visibly distinct (AC-30)', () => {
  it('enumerates exactly the four distinct claim-strength classes with distinct labels', () => {
    const keys = MAKER_EVIDENCE_CLASSES.map((c) => c.key);
    expect(new Set(keys)).toEqual(
      new Set(['own_reconciled_fill', 'observed_market_print', 'counterfactual', 'scored_arena']),
    );
    // labels must be pairwise-distinct — the classes are never conflated in copy
    const labels = MAKER_EVIDENCE_CLASSES.map((c) => c.label);
    expect(new Set(labels).size).toBe(MAKER_EVIDENCE_CLASSES.length);
  });

  it('marks own_reconciled_fill structurally unavailable at MM-R1 (REQ-052) — never fabricated', () => {
    const own = MAKER_EVIDENCE_CLASSES.find((c) => c.key === 'own_reconciled_fill');
    expect(own?.available).toBe(false);
  });
});

describe('deriveCounterfactualCapacity (AC-31) — bounded, labeled, never our fill/PnL/rank', () => {
  const printClaim: MakerCapacityClaim = {
    kind: 'observed_market_print',
    capacity_usd: 5000,
    matched_observed_liquidity_usd: 1200,
  };
  const cfClaim: MakerCapacityClaim = {
    kind: 'counterfactual',
    capacity_usd: 800,
    matched_observed_liquidity_usd: 1200,
  };

  it('labels a third-party print as counterfactual — never our own fill/PnL/rank', () => {
    const v = deriveCounterfactualCapacity(printClaim);
    expect(v.evidenceClass).toBe('observed_market_print');
    expect(v.badge).toBe('counterfactual');
    expect(v.label).toMatch(/third-party|observed market print/i);
    // structural honesty invariant — a print is NEVER our fill / PnL / a rank input
    expect(v.isOwnFill).toBe(false);
    expect(v.isPnl).toBe(false);
    expect(v.isRankInput).toBe(false);
  });

  it('labels a counterfactual capacity as a bounded ceiling — not a fill', () => {
    const v = deriveCounterfactualCapacity(cfClaim);
    expect(v.evidenceClass).toBe('counterfactual');
    expect(v.badge).toBe('counterfactual');
    expect(v.label).toMatch(/counterfactual|ceiling/i);
    expect(v.isOwnFill).toBe(false);
  });

  it('BOUNDS displayed capacity by matched observed liquidity (never claim more than was observed)', () => {
    const v = deriveCounterfactualCapacity(printClaim); // 5000 wanted, only 1200 observed
    expect(v.boundedCapacityUsd).toBe(1200);
    expect(v.isBounded).toBe(true);
  });

  it('leaves capacity unclamped when it is within matched observed liquidity', () => {
    const v = deriveCounterfactualCapacity(cfClaim); // 800 wanted, 1200 observed
    expect(v.boundedCapacityUsd).toBe(800);
    expect(v.isBounded).toBe(false);
  });
});
