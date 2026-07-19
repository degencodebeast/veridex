import { describe, it, expect } from 'vitest';
import {
  ARCHETYPES,
  SPORTS_ACTION_TYPES,
  MARKET_FAMILY_KEYS,
  toViewRuntimeEvent,
  isCanonicalChannel,
  type LeaderboardRow,
  type PolicyEnvelope,
  type RuntimeEvent,
  type RuntimeEventsResponse,
} from '@/lib/catalog';
import { PATHS } from '@/lib/api';
import type { RuntimeEvent as WireRuntimeEvent } from '@/lib/wire';

describe('catalog contracts (C2 §4 bind shapes; reuse C1, add C2-specific)', () => {
  it('freezes the five SportsActionTypes', () => {
    expect([...SPORTS_ACTION_TYPES]).toEqual([
      'WAIT', 'FLAG_VALUE', 'FOLLOW_MOMENTUM', 'FADE', 'WIDEN_OR_SUSPEND',
    ]);
  });

  it('freezes the five archetypes and the three market families', () => {
    expect([...ARCHETYPES]).toEqual(['value_clv', 'baseline', 'momentum', 'contrarian', 'stale_line']);
    expect([...MARKET_FAMILY_KEYS]).toEqual([
      '1X2_PARTICIPANT_RESULT', 'OVERUNDER_PARTICIPANT_GOALS', 'ASIANHANDICAP_PARTICIPANT_GOALS',
    ]);
  });

  it('accepts a LeaderboardRow REUSED from C1 (WD-7 superset: valid_count + clv_confidence + low_sample)', () => {
    const row: LeaderboardRow = {
      rank: 1, agent_id: 'value_clv', agent_name: 'Value CLV', agent_kind: 'numeric',
      runs: 12, avg_clv_bps: 18.4, total_clv_bps: 220.8, sim_pnl: 1.2, brier: 0.21,
      max_drawdown: -3.1, action_count: 96, valid_pct: 94.8,
      proof_mode: 'reproducible', eligibility_badge: 'eligible',
      anchor_status: 'anchored', source_mode: 'live',
      valid_count: 91, clv_confidence: 'high', low_sample: false,
    };
    expect(row.avg_clv_bps).toBe(18.4);
    expect(row.valid_count).toBe(91); // WD-7 source preserved from C1
  });

  it('accepts a PolicyEnvelope with the real fields', () => {
    const p: PolicyEnvelope = {
      max_stake: 100, max_orders_per_run: 5, max_orders_per_session: 20,
      max_orders_per_day: 50, venue_allowlist: ['sxbet'], market_allowlist: ['1X2_PARTICIPANT_RESULT'],
      min_edge_bps: 8, max_slippage_bps: 25, max_price: 4.5, max_quote_age_s: 30,
      cooldown_s: 10, human_approval_threshold: 250, kill_switch: false,
    };
    expect(p.min_edge_bps).toBe(8);
  });

  it('extends C1 PATHS with the OWNER-SCOPED runtime-events route (F-6: the public path is retired)', () => {
    expect(PATHS.leaderboard()).toBe('/leaderboard');
    expect(PATHS.competitionState('wc-fra-bra')).toBe('/competitions/wc-fra-bra');
    // F-6 retired the orphaned PUBLIC /agents/{id}/runtime-events; the live path is owner-scoped by
    // instance with an exclusive `since` cursor (veridex/api/router.py get_instance_runtime_events).
    expect((PATHS as Record<string, unknown>).runtimeEvents).toBeUndefined();
    expect(PATHS.instanceRuntimeEvents('inst_abc', 0)).toBe('/agents/instances/inst_abc/runtime-events?since=0');
  });

  it('maps a wire OPS RuntimeEvent → view RuntimeEvent, and OPS is NOT a canonical channel (SEC-003 seam)', () => {
    const wire: WireRuntimeEvent = {
      type: 'action_emitted', agent_id: 'momentum_fr', run_id: 'r1', session_id: null,
      ts: 1, channel: 'OPS', payload: { summary: 'BACK FRA 1X2 @ 2.38' },
    };
    const view: RuntimeEvent = toViewRuntimeEvent(wire);
    expect(view.kind).toBe('action_emitted'); // wire `type` → view `kind`
    expect(view.channel).toBe('OPS');
    // Invariant #1 seam: OPS telemetry is NEVER a canonical channel → hidden by the
    // canonical-only default filter (the drawer applies this in T14).
    expect(isCanonicalChannel('OPS')).toBe(false);
    expect(isCanonicalChannel('PROOF')).toBe(true);
    expect(isCanonicalChannel('POLICY')).toBe(true);
    expect(isCanonicalChannel('EXEC')).toBe(true);
    const resp: RuntimeEventsResponse = { events: [view] };
    expect(resp.events).toHaveLength(1);
  });
});
