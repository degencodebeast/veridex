import { describe, it, expect } from 'vitest';
import { rankByAvgClv, isEligible, numericClass, clvConfidence, isLowSample } from '@/lib/derive';
import type { LeaderboardRow } from '@/lib/contracts';

function row(p: Partial<LeaderboardRow>): LeaderboardRow {
  return {
    rank: 0, agent_id: 'a', agent_name: 'A', agent_kind: 'numeric', runs: 1,
    avg_clv_bps: 0, total_clv_bps: 0, sim_pnl: 0, brier: 0, max_drawdown: 0,
    action_count: 0, valid_count: 0, valid_pct: 0, proof_mode: 'reproducible',
    eligibility_badge: 'eligible', anchor_status: 'anchored', source_mode: 'replay',
    // C1 LeaderboardRow (WD-7 superset) requires these:
    clv_confidence: 'high', low_sample: false, ...p,
  };
}

describe('derive (SEC-005 / REQ-006 / WD-7)', () => {
  it('ranks by avg_clv_bps desc and a not-eligible high-CLV agent outranks an eligible one (AC-005)', () => {
    const ranked = rankByAvgClv([
      row({ agent_id: 'elig', avg_clv_bps: 5, eligibility_badge: 'eligible' }),
      row({ agent_id: 'notElig', avg_clv_bps: 20, eligibility_badge: 'not-eligible' }),
    ]);
    expect(ranked.map((r) => r.agent_id)).toEqual(['notElig', 'elig']);
    expect(ranked[0].rank).toBe(1);
    expect(ranked[1].rank).toBe(2);
  });

  it('ranks by avg_clv_bps and confidence/low_sample NEVER reorder (WD-7 is display-only, SEC-005)', () => {
    // A high-CLV agent with LOW confidence (tiny sample) must still rank above a
    // high-confidence lower-CLV agent — confidence is display, never a rank input.
    const ranked = rankByAvgClv([
      row({ agent_id: 'highConf', avg_clv_bps: 8, clv_confidence: 'high', low_sample: false, valid_count: 90 }),
      row({ agent_id: 'lowConf', avg_clv_bps: 25, clv_confidence: 'low', low_sample: true, valid_count: 3 }),
    ]);
    expect(ranked.map((r) => r.agent_id)).toEqual(['lowConf', 'highConf']);
  });

  it('does not mutate the input array', () => {
    const input = [row({ avg_clv_bps: 1 }), row({ avg_clv_bps: 2 })];
    const before = input.map((r) => r.avg_clv_bps);
    rankByAvgClv(input);
    expect(input.map((r) => r.avg_clv_bps)).toEqual(before);
  });

  it('derives eligibility from proof mode only (REQ-006)', () => {
    expect(isEligible('reproducible')).toBe(true);
    expect(isEligible('verified')).toBe(true);
    expect(isEligible('partial')).toBe(false);
  });

  it('maps numeric sign to a color class', () => {
    expect(numericClass(3.2)).toBe('pos');
    expect(numericClass(-0.1)).toBe('neg');
    expect(numericClass(0)).toBe('zero');
  });

  it('grades CLV confidence by sample size and flags low samples (WD-7)', () => {
    expect(clvConfidence(40)).toBe('high');
    expect(clvConfidence(12)).toBe('medium');
    expect(clvConfidence(4)).toBe('low');
    expect(isLowSample(4)).toBe(true);
    expect(isLowSample(10)).toBe(false);
  });
});
