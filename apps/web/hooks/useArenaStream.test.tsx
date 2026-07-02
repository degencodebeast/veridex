import { describe, it, expect, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useArenaStream, applyEvent } from '@/hooks/useArenaStream';
import { sampleCockpitState } from '@/__tests__/fixtures/contracts';
import type { CockpitState } from '@/lib/contracts';

// Capture the onEvent callback the hook registers so tests can drive it directly — a thin fake
// standing in for a real WebSocket, matching the ArenaSocketOptions contract in lib/ws.ts.
let lastOpts: { onEvent: (e: unknown) => void; onStatus?: (s: string) => void } | null = null;

// Stub ArenaSocket so the hook never opens a real WebSocket.
vi.mock('@/lib/ws', () => ({
  ArenaSocket: class {
    constructor(_url: string, opts: { onEvent: (e: unknown) => void; onStatus?: (s: string) => void }) {
      lastOpts = opts;
    }
    connect() {}
    close() {}
  },
}));

function stateFor(id: string, seqs: number[]): CockpitState {
  return {
    ...sampleCockpitState,
    competition_id: id,
    events: seqs.map((seq) => ({ seq, type: 'AGENT_ACTION', payload_hash: '0x', evidence: true, ts: seq })),
  };
}

describe('useArenaStream (cross-competition isolation)', () => {
  it('resets the projection when competitionId changes (no stale-event contamination)', () => {
    const a = stateFor('comp-a', [10, 11, 12]);
    const b = stateFor('comp-b', []);
    const { result, rerender } = renderHook(
      ({ id, init }) => useArenaStream(id, init),
      { initialProps: { id: 'comp-a', init: a } },
    );
    expect(result.current.state.competition_id).toBe('comp-a');
    expect(result.current.state.events.map((e) => e.seq)).toEqual([10, 11, 12]);

    // Navigate /arena/comp-a -> /arena/comp-b (App Router reuses the component).
    rerender({ id: 'comp-b', init: b });
    expect(result.current.state.competition_id).toBe('comp-b'); // reset, not stale comp-a
    expect(result.current.state.events).toEqual([]); // comp-a's events must not carry over
  });
});

describe('applyEvent (pure projection helper)', () => {
  it('pushes a policy_result event onto state.policy in addition to the event log (T10 AC-2D-104)', () => {
    const base: CockpitState = { ...sampleCockpitState, events: [], policy: [] };
    const decision = { tick_seq: 6, decision: 'ALLOW' as const, reason: 'edge >= min', edge_bps: 22, min_edge_bps: 8 };
    const withPolicy = applyEvent(base, {
      seq: 1, type: 'policy_result', payload_hash: '0x1', evidence: false, ts: 1, policy: decision,
    });
    expect(withPolicy.policy).toEqual([decision]);
    expect(withPolicy.events.map((e) => e.seq)).toEqual([1]);
  });

  it('leaves state.policy untouched for a non-policy event', () => {
    const base: CockpitState = { ...sampleCockpitState, events: [], policy: [] };
    const next = applyEvent(base, { seq: 1, type: 'AGENT_ACTION', payload_hash: '0x1', evidence: true, ts: 1 });
    expect(next.policy).toEqual([]);
  });
});

describe('useArenaStream — feed health projection (T10 AC-2D-103/104)', () => {
  it('reports connected/ws_live honestly from wsStatus and increments ticks_seen on market_tick events', () => {
    const initial = stateFor('comp-a', []);
    const { result } = renderHook(() => useArenaStream('comp-a', initial, {
      source_mode: 'live', ws_live: false, connected: false, txline_configured: true,
      events_per_min: null, ticks_seen: 0, staleness_s: null, stale: true, fixture_id: 1,
      anchor_status: 'not_applicable', last_tick_ts: null,
    }));

    expect(result.current.feedHealth.connected).toBe(false); // honest until the socket opens

    act(() => { lastOpts?.onStatus?.('connected'); });
    expect(result.current.feedHealth.connected).toBe(true);
    expect(result.current.feedHealth.ws_live).toBe(true);

    act(() => {
      lastOpts?.onEvent({ seq: 0, type: 'MARKET_TICK', payload_hash: '0x', evidence: true, ts: 1 });
    });
    expect(result.current.feedHealth.ticks_seen).toBe(1);
  });

  it('never claims the feed is fresh while disconnected/reconnecting (no frozen stale-as-live view)', () => {
    const initial = stateFor('comp-a', []);
    const { result } = renderHook(() => useArenaStream('comp-a', initial, {
      source_mode: 'live', ws_live: true, connected: true, txline_configured: true,
      events_per_min: null, ticks_seen: 4, staleness_s: 1, stale: false, fixture_id: 1,
      anchor_status: 'not_applicable', last_tick_ts: 10,
    }));

    act(() => { lastOpts?.onStatus?.('disconnected'); });
    expect(result.current.feedHealth.connected).toBe(false);
    expect(result.current.feedHealth.stale).toBe(true); // honest: disconnected can never report fresh
  });
});
