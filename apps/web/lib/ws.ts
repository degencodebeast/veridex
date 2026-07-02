// WebSocket projection client for the canonical event log (CON-002). It NEVER
// silently drops: on a sequence gap or slow-client buffer overflow it emits a
// gap notice and disconnects so the cockpit can resync from the persisted log.
// SEC-003: this stream carries CANONICAL events only — RuntimeEvent ops telemetry
// is a separate channel and never enters this projection.
import type { CanonicalEvent } from '@/lib/contracts';

export const OVERFLOW_BUFFER_MAX = 512;

export function detectSeqGap(lastSeq: number, incomingSeq: number): boolean {
  if (lastSeq < 0) return false; // first event
  return incomingSeq !== lastSeq + 1;
}

export type GapReason = 'sequence-gap' | 'slow-client-overflow';
export interface GapInfo { reason: GapReason; lastSeq: number; incomingSeq: number }

export interface ArenaSocketOptions {
  socketFactory?: (url: string) => WebSocket;
  onEvent: (event: CanonicalEvent) => void;
  onGap?: (info: GapInfo) => void;
  onStatus?: (status: 'connecting' | 'connected' | 'disconnected') => void;
  bufferMax?: number;
}

export class ArenaSocket {
  private ws: WebSocket | null = null;
  private lastSeq = -1;
  private buffer: CanonicalEvent[] = [];
  private readonly bufferMax: number;

  constructor(private url: string, private opts: ArenaSocketOptions) {
    this.bufferMax = opts.bufferMax ?? OVERFLOW_BUFFER_MAX;
  }

  connect(): void {
    this.opts.onStatus?.('connecting');
    const factory = this.opts.socketFactory ?? ((u: string) => new WebSocket(u));
    this.ws = factory(this.url);
    this.ws.onopen = () => this.opts.onStatus?.('connected');
    this.ws.onclose = () => this.opts.onStatus?.('disconnected');
    this.ws.onmessage = (e: MessageEvent) => this.ingest(e.data as string);
  }

  private ingest(raw: string): void {
    let event: CanonicalEvent;
    // A malformed/unparseable frame is dropped here — but this is NOT a silent
    // canonical drop: lastSeq is unchanged, so the next valid event whose seq isn't
    // contiguous trips detectSeqGap and fails closed (disconnect + resync). CON-002.
    try { event = JSON.parse(raw) as CanonicalEvent; } catch { return; }

    if (detectSeqGap(this.lastSeq, event.seq)) {
      this.fail('sequence-gap', event.seq);
      return;
    }
    this.lastSeq = event.seq;

    // Enqueue, then drain in order. We only remove an event AFTER the consumer
    // accepts it — so a stalled consumer makes the buffer grow until it exceeds
    // its cap, at which point we disconnect rather than dropping events (CON-002).
    this.buffer.push(event);
    if (this.buffer.length > this.bufferMax) {
      this.fail('slow-client-overflow', event.seq);
      return;
    }
    this.drain();
  }

  private drain(): void {
    while (this.buffer.length > 0) {
      const next = this.buffer[0];
      this.opts.onEvent(next); // may throw if the consumer is stalled — leaves it buffered
      this.buffer.shift();
    }
  }

  private fail(reason: GapReason, incomingSeq: number): void {
    this.opts.onGap?.({ reason, lastSeq: this.lastSeq, incomingSeq });
    this.close(reason);
  }

  close(reason = 'client-close'): void {
    this.ws?.close(4000, reason);
    this.ws = null;
  }
}
