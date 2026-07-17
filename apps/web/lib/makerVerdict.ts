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
import type { MakerFalsification } from '@/lib/contracts';
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
