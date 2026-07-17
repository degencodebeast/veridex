// Adversarial Maker Arena fixtures (I-R remediation). The sealed fixture is SEPARATED with
// scored == quote_count, so it masks the contract branches that make false claims dangerous
// (Findings M1/M4). These variants exercise the OTHER verdicts the backend contract permits
// (veridex/maker/falsification.py: SEPARATED | INCONCLUSIVE | INVERTED) and unequal scored
// counts — every variant is derived from the real sealed view-model, never hand-authored shapes.
import { MAKER_ARENA_RESULT } from '@/lib/fixtures/maker';
import type { MakerArenaResultView, MakerFalsification } from '@/lib/contracts';

export function makerResultWith(over: {
  falsification?: Partial<MakerFalsification>;
  scoredByAgent?: Record<string, number>;
}): MakerArenaResultView {
  const base = structuredClone(MAKER_ARENA_RESULT);
  if (over.falsification) {
    // The verdict is carried in BOTH the result and its proof_card — patch the pair together
    // so a variant can never render one verdict on the board and another on the card.
    base.falsification = { ...base.falsification, ...over.falsification };
    base.proof_card.falsification = { ...base.proof_card.falsification, ...over.falsification };
  }
  if (over.scoredByAgent) {
    base.leaderboard = base.leaderboard.map((r) =>
      over.scoredByAgent![r.agent_id] === undefined ? r : { ...r, scored: over.scoredByAgent![r.agent_id] },
    );
  }
  return base;
}

// CI spans zero → no winner may be crowned.
export const MAKER_INCONCLUSIVE = makerResultWith({
  falsification: { verdict: 'INCONCLUSIVE', headline: 'INCONCLUSIVE_QUOTE_QUALITY', delta_bps: 10, ci_low_bps: -5, ci_high_bps: 25 },
});

// Whole CI below zero → the candidate is reliably WORSE than the control.
export const MAKER_INVERTED = makerResultWith({
  falsification: { verdict: 'INVERTED', headline: 'INVERTED_QUOTE_QUALITY', delta_bps: -43, ci_low_bps: -52, ci_high_bps: -34 },
});

// A verdict outside the contract vocabulary → render an honest no-claim state, never a win.
export const MAKER_UNKNOWN_VERDICT = makerResultWith({
  falsification: { verdict: 'BOGUS_FUTURE_VERDICT', headline: 'BOGUS', delta_bps: 1, ci_low_bps: -1, ci_high_bps: 2 },
});
