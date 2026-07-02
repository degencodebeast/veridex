import { describe, it, expect } from 'vitest';
import { detectSeqGap, OVERFLOW_BUFFER_MAX, ArenaSocket } from '@/lib/ws';

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
