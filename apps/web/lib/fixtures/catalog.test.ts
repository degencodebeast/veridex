import { describe, it, expect } from 'vitest';
import {
  LEADERBOARD_ROWS, COMPETITIONS, AGENTS, AGENT_PROFILES, RUNTIME_OVERVIEW, RUNTIME_LOG, MY_REWARDS,
} from '@/lib/fixtures/catalog';

describe('fixtures (typed realistic fiction)', () => {
  it('includes a not-eligible row with the highest avg_clv_bps (AC-005 coverage)', () => {
    const top = [...LEADERBOARD_ROWS].sort((a, b) => b.avg_clv_bps - a.avg_clv_bps)[0];
    expect(top.eligibility_badge).toBe('not-eligible');
  });

  it('includes a low-sample row (WD-7 coverage)', () => {
    expect(LEADERBOARD_ROWS.some((r) => r.valid_count < 10)).toBe(true);
  });

  it('keeps clv_confidence honest to the valid sample size (WD-7: low sample => low/low_sample)', () => {
    const lowRow = LEADERBOARD_ROWS.find((r) => r.valid_count < 10)!;
    expect(lowRow.clv_confidence).toBe('low');
    expect(lowRow.low_sample).toBe(true);
  });

  it('covers all three competition lifecycles', () => {
    const states = new Set(COMPETITIONS.map((c) => c.lifecycle));
    expect(states).toEqual(new Set(['live', 'upcoming', 'settled']));
  });

  it('exposes a BYOA runtime overview with null optional fields (AC-030)', () => {
    const byoa = Object.values(RUNTIME_OVERVIEW).find((o) => o.source === 'BYOA')!;
    expect(byoa.latest_model_latency_ms).toBeNull();
    expect(byoa.latest_model_tokens).toBeNull();
  });

  it('has a runtime log with at least one OPS line and one PROOF line (AC-003)', () => {
    expect(RUNTIME_LOG.some((l) => l.channel === 'OPS')).toBe(true);
    expect(RUNTIME_LOG.some((l) => l.channel === 'PROOF')).toBe(true);
  });

  it('keys agent profiles by an existing agent id', () => {
    const id = AGENTS[0].agent_id;
    expect(AGENT_PROFILES[id]).toBeDefined();
  });

  it('does NOT overclaim rewards — no fabricated paid payouts, amounts are honestly absent', () => {
    // No-overclaim: fiction must never invent a settled dollar payout. Every reward is an
    // honest "—" placeholder tied to a non-paid state (design-target / pending).
    MY_REWARDS.forEach((r) => {
      expect(r.payout_state).not.toBe('paid');
      expect(r.amount_label.startsWith('—')).toBe(true);
    });
  });
});
