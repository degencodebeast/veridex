import { describe, it, expect } from 'vitest';
import { detectSeqGap, OVERFLOW_BUFFER_MAX, ArenaSocket, normalizeWireEvent } from '@/lib/ws';

// Minimal fake WebSocket capturing close() + letting tests push messages.
class FakeSocket {
  static OPEN = 1;
  readyState = 1;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number; reason: string }) => void) | null = null;
  closed: { code: number; reason: string } | null = null;
  send() {}
  close(code = 1000, reason = '') { this.readyState = 3; this.closed = { code, reason }; this.onclose?.({ code, reason }); }
  emit(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) }); }
}

describe('detectSeqGap (CON-002 gap detection)', () => {
  it('is false for the first event and for contiguous sequences', () => {
    expect(detectSeqGap(-1, 0)).toBe(false);
    expect(detectSeqGap(41, 42)).toBe(false);
  });
  it('is true when a sequence is skipped', () => {
    expect(detectSeqGap(41, 43)).toBe(true);
  });
});

describe('ArenaSocket (CON-002: never silently drop)', () => {
  it('delivers contiguous events in order', () => {
    const fake = new FakeSocket();
    const events: number[] = [];
    const sock = new ArenaSocket('wss://x/arena/c1', {
      socketFactory: () => fake as unknown as WebSocket,
      onEvent: (e) => events.push(e.seq),
    });
    sock.connect();
    fake.onopen?.();
    fake.emit({ seq: 0, type: 'AGENT_ACTION', payload_hash: '0x', evidence: true, ts: 1 });
    fake.emit({ seq: 1, type: 'law_recomputed', payload_hash: '0x', evidence: true, ts: 2 });
    expect(events).toEqual([0, 1]);
  });

  it('disconnects with a gap reason on a sequence gap (no silent drop)', () => {
    const fake = new FakeSocket();
    const gaps: string[] = [];
    const sock = new ArenaSocket('wss://x', {
      socketFactory: () => fake as unknown as WebSocket,
      onEvent: () => {},
      onGap: (info) => gaps.push(info.reason),
    });
    sock.connect();
    fake.onopen?.();
    fake.emit({ seq: 0, type: 'a', payload_hash: '0x', evidence: true, ts: 1 });
    fake.emit({ seq: 2, type: 'b', payload_hash: '0x', evidence: true, ts: 2 }); // gap!
    expect(gaps).toContain('sequence-gap');
    expect(fake.closed?.reason).toBe('sequence-gap');
  });

  it('disconnects on slow-client buffer overflow rather than dropping', () => {
    const fake = new FakeSocket();
    let overflow = '';
    const sock = new ArenaSocket('wss://x', {
      socketFactory: () => fake as unknown as WebSocket,
      onEvent: () => { throw new Error('consumer stalled'); }, // simulate a stalled consumer
      onGap: (info) => { overflow = info.reason; },
      bufferMax: 4,
    });
    sock.connect();
    fake.onopen?.();
    for (let i = 0; i < 10; i++) {
      try { fake.emit({ seq: i, type: 't', payload_hash: '0x', evidence: false, ts: i }); } catch { /* stalled */ }
    }
    expect(overflow).toBe('slow-client-overflow');
    expect(fake.closed?.reason).toBe('slow-client-overflow');
    expect(OVERFLOW_BUFFER_MAX).toBe(512);
  });
});

// normalizeWireEvent maps the REAL backend CompetitionEvent wire frame (event_type/event_ts/
// payload — see veridex/competition/events.py) into the frontend CanonicalEvent view-model, while
// staying backward-compatible with the simplified {type,ts} fixture shape used above.
describe('normalizeWireEvent (real backend wire ⇒ CanonicalEvent view-model)', () => {
  it('maps event_type/event_ts and normalizes agent_action to AGENT_ACTION (deep-link contract)', () => {
    const event = normalizeWireEvent({
      seq: 5, event_type: 'agent_action', event_ts: 1719663793, evidence: true,
      payload_hash: '0xdead', payload: { agent_id: 'agt_1', action: 'FLAG_VALUE', market_key: '1X2', side: 'FRA' },
    });
    expect(event.type).toBe('AGENT_ACTION');
    expect(event.ts).toBe(1719663793);
    expect(event.agent_id).toBe('agt_1');
  });

  it('is backward-compatible with the simplified {type, ts} shape (no payload/event_type)', () => {
    const event = normalizeWireEvent({ seq: 1, type: 'score_update', ts: 42, evidence: false, payload_hash: '0xaa' });
    expect(event.type).toBe('score_update');
    expect(event.ts).toBe(42);
    expect(event.clv).toBeUndefined();
  });

  it('derives a true-CLV cell from a law_result payload carrying clv_bps', () => {
    const event = normalizeWireEvent({
      seq: 9, event_type: 'law_result', event_ts: 1, evidence: false, payload_hash: '0xbb',
      payload: { agent_id: 'a', tick_seq: 3, clv_bps: 18, valid: true, reason: 'ok', recomputed_edge_bps: 22 },
    });
    expect(event.clv).toEqual({ kind: 'clv', bps: 18 });
  });

  it('derives a window-CLV cell (never the true-CLV cell) from a law_result payload carrying window_clv_bps', () => {
    const event = normalizeWireEvent({
      seq: 10, event_type: 'law_result', event_ts: 1, evidence: false, payload_hash: '0xcc',
      payload: { agent_id: 'a', tick_seq: 4, window_clv_bps: 7, valid: true, reason: 'ok', recomputed_edge_bps: 9 },
    });
    expect(event.clv).toEqual({ kind: 'window_clv', bps: 7 });
  });

  it('derives an honest pending cell (no fabricated number) for a pending_horizon law_result row', () => {
    const event = normalizeWireEvent({
      seq: 11, event_type: 'law_result', event_ts: 1, evidence: false, payload_hash: '0xdd',
      payload: { agent_id: 'a', tick_seq: 5, clv_bps: 'pending', valid: true, reason: 'pending_horizon', recomputed_edge_bps: 0 },
    });
    expect(event.clv).toEqual({ kind: 'pending' });
  });

  it('extracts a PolicyDecision from a policy_result payload', () => {
    const event = normalizeWireEvent({
      seq: 12, event_type: 'policy_result', event_ts: 1, evidence: false, payload_hash: '0xee',
      payload: { tick_seq: 6, decision: 'ALLOW', reason: 'edge >= min', edge_bps: 22, min_edge_bps: 8 },
    });
    expect(event.policy).toEqual({ tick_seq: 6, decision: 'ALLOW', reason: 'edge >= min', edge_bps: 22, min_edge_bps: 8 });
  });
});
