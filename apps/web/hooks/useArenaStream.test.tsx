import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useArenaStream, applyEvent } from '@/hooks/useArenaStream';
import { sampleCockpitState } from '@/__tests__/fixtures/contracts';
import type { CockpitState } from '@/lib/contracts';

// Capture the onEvent callback the hook registers so tests can drive it directly — a thin fake
// standing in for a real WebSocket, matching the ArenaSocketOptions contract in lib/ws.ts.
let lastOpts: { onEvent: (e: unknown) => void; onStatus?: (s: string) => void } | null = null;
// Every URL the hook constructed an ArenaSocket with — lets reconnect tests assert a fresh
// subscription actually happened (not just that wsStatus flipped).
let constructedUrls: string[] = [];

// Stub ArenaSocket so the hook never opens a real WebSocket. `connect()` mirrors the REAL
// ArenaSocket.connect() (lib/ws.ts), which synchronously fires onStatus('connecting') first —
// a no-op connect() here would hide bugs in how the hook reacts to that transition (FOLD 1/2).
vi.mock('@/lib/ws', () => ({
  ArenaSocket: class {
    private opts: { onEvent: (e: unknown) => void; onStatus?: (s: string) => void };
    constructor(url: string, opts: { onEvent: (e: unknown) => void; onStatus?: (s: string) => void }) {
      constructedUrls.push(url);
      this.opts = opts;
      lastOpts = opts;
    }
    connect() { this.opts.onStatus?.('connecting'); }
    close() {}
  },
}));

beforeEach(() => {
  lastOpts = null;
  constructedUrls = [];
});

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

  it('resets leaderboard + receipts when competitionId changes (no stale board/receipt carry-over)', () => {
    const a: CockpitState = {
      ...stateFor('comp-a', []),
      leaderboard: [{
        rank: 1, agent_id: 'a', agent_name: 'A', agent_kind: '', runs: 0, avg_clv_bps: 9, total_clv_bps: 9,
        sim_pnl: null, brier: null, max_drawdown: null, action_count: null, valid_pct: null,
        proof_mode: 'reproducible', eligibility_badge: 'eligible', anchor_status: 'pending',
        source_mode: 'live', valid_count: 2, clv_confidence: '', low_sample: false,
      }],
      receipts: [{
        execution_id: 'e1', venue: 'sxbet', market_ref: '1X2:FRA', side: 'back', requested_size: 1,
        filled_size: 1, price: 1.5, status: 'filled', venue_order_id: 'o1', mode: 'paper',
        submitted_at: 1, settled_at: 2,
      }],
    };
    const b: CockpitState = { ...stateFor('comp-b', []), leaderboard: [], receipts: [] }; // fresh competition
    const { result, rerender } = renderHook(
      ({ id, init }) => useArenaStream(id, init),
      { initialProps: { id: 'comp-a', init: a } },
    );
    expect(result.current.state.leaderboard.length).toBe(1);
    expect(result.current.state.receipts.length).toBe(1);

    rerender({ id: 'comp-b', init: b });
    expect(result.current.state.leaderboard).toEqual([]); // comp-a's board must not carry over
    expect(result.current.state.receipts).toEqual([]);    // comp-a's receipts must not carry over
  });
});

// F-5 RED-1/RED-4: a live SCORE_UPDATE must refetch the COMPETITION-SCOPED state and project the
// backend-authoritative leaderboard + sealed receipts into the cockpit (blank → populated in live).
describe('useArenaStream — SCORE_UPDATE refetches the competition-scoped leaderboard + receipts', () => {
  const populatedRefetch = (id: string): Promise<CockpitState> => Promise.resolve({
    ...stateFor(id, []),
    leaderboard: [{
      rank: 1, agent_id: 'alpha', agent_name: 'alpha', agent_kind: '', runs: 0, avg_clv_bps: 92, total_clv_bps: 184,
      sim_pnl: null, brier: null, max_drawdown: null, action_count: null, valid_pct: null,
      proof_mode: 'reproducible', eligibility_badge: 'eligible', anchor_status: 'pending',
      source_mode: 'live', valid_count: 2, clv_confidence: '', low_sample: false,
    }],
    receipts: [{
      execution_id: 'exec-1', venue: 'sxbet', market_ref: '1X2:FRA', side: 'back', requested_size: 100,
      filled_size: 100, price: 1.472, status: 'filled', venue_order_id: 'ord-1', mode: 'paper',
      submitted_at: 1782518396, settled_at: 1782518397,
    }],
  });

  it('populates the blank live board + receipts from the refetch on a score_update event', async () => {
    const initial: CockpitState = { ...stateFor('comp-a', []), leaderboard: [], receipts: [] };
    const refetch = vi.fn(populatedRefetch);
    const { result } = renderHook(() => useArenaStream('comp-a', initial, undefined, refetch));

    expect(result.current.state.leaderboard).toEqual([]); // blank until the stream drives a score

    await act(async () => {
      lastOpts?.onEvent({ seq: 1, type: 'score_update', payload_hash: '0x', evidence: false, ts: 1 });
    });

    expect(refetch).toHaveBeenCalledWith('comp-a'); // competition-scoped refetch, by id
    expect(result.current.state.leaderboard.length).toBe(1);
    expect(result.current.state.leaderboard[0].agent_id).toBe('alpha');
    expect(result.current.state.receipts.length).toBe(1);
    expect(result.current.state.receipts[0].execution_id).toBe('exec-1');
  });

  it('does NOT refetch for a non-score event (only SCORE_UPDATE drives the board refetch)', async () => {
    const initial: CockpitState = { ...stateFor('comp-a', []), leaderboard: [], receipts: [] };
    const refetch = vi.fn(populatedRefetch);
    renderHook(() => useArenaStream('comp-a', initial, undefined, refetch));

    await act(async () => {
      lastOpts?.onEvent({ seq: 1, type: 'AGENT_ACTION', payload_hash: '0x', evidence: true, ts: 1 });
    });

    expect(refetch).not.toHaveBeenCalled();
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

  // FOLD 1/2 (code-review): the real ArenaSocket.connect() synchronously fires
  // onStatus('connecting') BEFORE 'connected' ever arrives. That transition forces stale:true
  // (honesty-safe), but nothing then cleared it back to false once the feed proved itself live —
  // so feedHealth.stale was stuck true FOREVER after the first connect, even on a healthy,
  // ticking feed (FeedHealthPanel's "feed stale" branch never went away). A live MARKET_TICK is
  // the freshest possible proof of liveness and must clear it.
  it('clears stale once a connected feed delivers a live tick — never permanently stuck stale', () => {
    const initial = stateFor('comp-a', []);
    const { result } = renderHook(() => useArenaStream('comp-a', initial, {
      source_mode: 'live', ws_live: false, connected: false, txline_configured: true,
      events_per_min: null, ticks_seen: 0, staleness_s: null, stale: true, fixture_id: 1,
      anchor_status: 'not_applicable', last_tick_ts: null,
    }));

    // Real sequence: connecting (fired by the mock's connect(), matching lib/ws.ts) -> connected -> tick.
    act(() => { lastOpts?.onStatus?.('connected'); });
    act(() => {
      lastOpts?.onEvent({ seq: 0, type: 'MARKET_TICK', payload_hash: '0x', evidence: true, ts: 1 });
    });

    expect(result.current.feedHealth.connected).toBe(true);
    expect(result.current.feedHealth.stale).toBe(false); // a healthy ticking feed must render feed-ok

    // Honesty-safe direction preserved: a later disconnect forces stale back to true — a dead
    // feed is never shown as live just because it was fresh a moment ago.
    act(() => { lastOpts?.onStatus?.('disconnected'); });
    expect(result.current.feedHealth.stale).toBe(true);
  });
});

// FOLD 3 (coverage gap): pins that a disconnect ACTUALLY re-subscribes (not just flips wsStatus)
// — a fresh ArenaSocket constructed with `?since_seq=<lastSeq>` after the fixed reconnect delay,
// so the server's gapless since_seq replay (api/ws.py) is what recovers a dropped spectator.
describe('useArenaStream — reconnect resubscribes with since_seq (FOLD 3)', () => {
  it('constructs a fresh socket with ?since_seq=<lastSeq> after the fixed reconnect delay', () => {
    vi.useFakeTimers();
    try {
      const initial = stateFor('comp-a', []);
      renderHook(() => useArenaStream('comp-a', initial));
      const urlsAtMount = constructedUrls.length;

      // Observe a real seq so the resubscribe has something concrete to replay from.
      act(() => {
        lastOpts?.onEvent({ seq: 7, type: 'MARKET_TICK', payload_hash: '0x', evidence: true, ts: 1 });
      });

      act(() => { lastOpts?.onStatus?.('disconnected'); });
      // Not immediate — the resubscribe is scheduled, not fired synchronously on disconnect.
      expect(constructedUrls.length).toBe(urlsAtMount);

      act(() => { vi.advanceTimersByTime(1000); });

      expect(constructedUrls.length).toBe(urlsAtMount + 1);
      expect(constructedUrls[constructedUrls.length - 1]).toMatch(/since_seq=7/);
    } finally {
      vi.useRealTimers();
    }
  });
});
