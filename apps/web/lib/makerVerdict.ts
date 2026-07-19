// Maker falsification verdict → render treatment (I-R M1). The ONE derivation every maker
// surface (Leaderboard strip / Duel result + cards / Maker Proof Card lead) binds to, so no
// screen can collapse the three-state contract into a boolean "SEPARATED vs everything else".
// Contract vocabulary (veridex/maker/falsification.py — delta = candidate − control quality):
//   SEPARATED    whole 95% CI above zero → the candidate is reliably LESS toxic (candidate wins)
//   INVERTED     whole 95% CI below zero → the candidate is reliably MORE toxic (control is the
//                less-toxic side; the candidate must never be presented as separated/safer)
//   INCONCLUSIVE CI spans zero → no winner may be crowned
// Anything else renders as an honest no-claim state (raw verdict shown verbatim, no winner) —
// never a fabricated win. Copy here derives from the REAL verdict, never a hardcoded happy path.
import type { MakerCapacityClaim, MakerFalsification } from '@/lib/contracts';
import type { BadgeVariant } from '@/lib/badges';

export interface MakerVerdictView {
  kind: 'SEPARATED' | 'INCONCLUSIVE' | 'INVERTED' | 'UNKNOWN';
  badge: BadgeVariant;       // separated | inconclusive | inverted
  badgeText: string | null;  // non-null only for UNKNOWN — the raw wire verdict, verbatim
  winner: 'candidate' | 'control' | null; // the reliably-less-toxic side; null = crown NO ONE
  headline: string;
  ciSub: string;             // the CI interpretation of the REAL verdict
}

export function deriveMakerVerdict(
  f: MakerFalsification,
  names: { candidate: string; control: string },
): MakerVerdictView {
  const { candidate, control } = names;
  switch (f.verdict) {
    case 'SEPARATED':
      return {
        kind: 'SEPARATED', badge: 'separated', badgeText: null, winner: 'candidate',
        headline: `${candidate} is less toxic than the ${control} control`,
        ciSub: 'whole 95% CI above zero → the difference is real',
      };
    case 'INCONCLUSIVE':
      return {
        kind: 'INCONCLUSIVE', badge: 'inconclusive', badgeText: null, winner: null,
        headline: `No separation between ${candidate} and the ${control} control`,
        ciSub: 'CI spans zero → the difference is not distinguishable from noise',
      };
    case 'INVERTED':
      return {
        kind: 'INVERTED', badge: 'inverted', badgeText: null, winner: 'control',
        headline: `Inverted — ${candidate} is reliably MORE toxic than the ${control} control`,
        ciSub: 'whole 95% CI below zero → the candidate is reliably worse than the control',
      };
    default:
      return {
        kind: 'UNKNOWN', badge: 'inconclusive', badgeText: f.verdict, winner: null,
        headline: 'Unrecognized falsification verdict — no winner is crowned',
        ciSub: 'verdict outside the contract vocabulary — no CI claim rendered',
      };
  }
}

// ── AC-30/AC-31 · Evidence-class honesty (Gate-B REQ-026/027/052/053) ────────────────────────────
// The four evidence classes a maker surface may display, ordered by claim strength. The invariant is
// structural, not editorial: a weaker class can NEVER be rendered as a stronger one — a third-party
// print / counterfactual ceiling is not our fill, our PnL, or a rank input. `own_reconciled_fill` is
// structurally UNAVAILABLE at MM-R1 (REQ-052) — it is listed so the taxonomy is complete and the
// screens can prove they never fabricate it, never emitted from real data here.
export type MakerEvidenceClass =
  | 'own_reconciled_fill'   // our own reconciled fill — the strongest claim; unavailable at MM-R1
  | 'observed_market_print' // a third party traded at a price — not our fill; capacity is a ceiling
  | 'counterfactual'        // what the OBSERVED book would have cleared — a bounded ceiling, not a fill
  | 'scored_arena';         // our sealed MM-R1 scored evidence (toxicity rank axis / markout diagnostic)

export interface MakerEvidenceClassMeta {
  key: MakerEvidenceClass;
  label: string;
  claimCeiling: string; // the strongest honest claim this class may make
  available: boolean;   // false ⇒ structurally unavailable at MM-R1 (never fabricated)
}

export const MAKER_EVIDENCE_CLASSES: readonly MakerEvidenceClassMeta[] = [
  {
    key: 'own_reconciled_fill',
    label: 'Own reconciled fill',
    claimCeiling: 'our own fill/PnL — structurally unavailable at MM-R1 (REQ-052), never fabricated',
    available: false,
  },
  {
    key: 'observed_market_print',
    label: 'Observed market print',
    claimCeiling: 'a third party traded at a price — never our fill, PnL, or capacity (REQ-027)',
    available: true,
  },
  {
    key: 'counterfactual',
    label: 'Counterfactual ceiling',
    claimCeiling: 'what the observed book would have cleared — a bounded ceiling, never a fill',
    available: true,
  },
  {
    key: 'scored_arena',
    label: 'Sealed arena score',
    claimCeiling: 'adverse-selection toxicity (rank axis) / markout (diagnostic) — never CLV/PnL',
    available: true,
  },
] as const;

// The render treatment for a historical capacity claim. Every boolean below is a HARD false: a
// counterfactual/print is never our fill, never PnL, never a rank input — no code path flips these.
export interface CounterfactualCapacityView {
  evidenceClass: 'observed_market_print' | 'counterfactual';
  badge: BadgeVariant;         // always 'counterfactual'
  label: string;               // human copy naming the class + its ceiling
  boundedCapacityUsd: number;  // min(capacity, matched observed liquidity) — the CEILING shown
  isBounded: boolean;          // true when the raw capacity was clamped DOWN to observed liquidity
  isOwnFill: false;            // structural: a print/counterfactual is NEVER our fill
  isPnl: false;                // structural: NEVER a PnL claim
  isRankInput: false;          // structural: NEVER feeds the rank axis
}

export function deriveCounterfactualCapacity(claim: MakerCapacityClaim): CounterfactualCapacityView {
  // BOUND the displayed capacity by matched observed liquidity — never claim more than was observed.
  const boundedCapacityUsd = Math.min(claim.capacity_usd, claim.matched_observed_liquidity_usd);
  const isBounded = claim.capacity_usd > claim.matched_observed_liquidity_usd;
  const label = claim.kind === 'observed_market_print'
    ? 'Observed market print — a third-party trade, never our fill / PnL / rank'
    : 'Counterfactual capacity — a bounded ceiling on the observed book, never a fill';
  return {
    evidenceClass: claim.kind,
    badge: 'counterfactual',
    label,
    boundedCapacityUsd,
    isBounded,
    isOwnFill: false,
    isPnl: false,
    isRankInput: false,
  };
}
