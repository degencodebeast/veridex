// WebSocket projection client for the canonical event log (CON-002). It NEVER
// silently drops: on a sequence gap or slow-client buffer overflow it emits a
// gap notice and disconnects so the cockpit can resync from the persisted log.
// SEC-003: this stream carries CANONICAL events only — RuntimeEvent ops telemetry
// is a separate channel and never enters this projection.
import type { CanonicalEvent, PolicyDecision } from '@/lib/contracts';

export const OVERFLOW_BUFFER_MAX = 512;

// The REAL backend wire frame is CompetitionEvent.model_dump(mode="json")
// (veridex/competition/events.py): {seq, event_type, event_ts, evidence, payload, payload_hash,
// ...}. `type`/`ts` are accepted too so existing simplified test fixtures keep working unchanged.
interface WireEvent {
  seq: number;
  event_type?: string;
  type?: string;
  event_ts?: number;
  ts?: number;
  evidence: boolean;
  payload_hash: string;
  payload?: Record<string, unknown>;
  agent_id?: string;
  summary?: string;
}

// The frontend's own display convention uppercases the evidence-bound event types (matches the
// existing AGENT_ACTION deep-link check in CanonicalEventStream); derived tail types (policy_result,
// score_update, ...) already match the backend's lowercase EventType values 1:1, so no alias needed.
const TYPE_ALIASES: Record<string, string> = { agent_action: 'AGENT_ACTION', market_tick: 'MARKET_TICK' };

function deriveSummary(rawType: string, payload: Record<string, unknown>): string | undefined {
  switch (rawType) {
    case 'agent_action':
      if (payload.error) return `error: ${payload.message ?? ''}`.trim();
      return [payload.action, payload.market_key, payload.side].filter(Boolean).join(' ') || undefined;
    case 'market_tick':
      return typeof payload.tick_seq === 'number' ? `tick ${payload.tick_seq}` : undefined;
    case 'policy_result':
      return typeof payload.decision === 'string' ? payload.decision : undefined;
    default:
      return undefined;
  }
}

// A `law_result` payload carries EXACTLY ONE of: true `clv_bps` (pre_match window), windowed
// `window_clv_bps` (fixed_duration/manual_stop — NEVER the same as true CLV), or the "pending"
// sentinel (`clv_bps === "pending"` / `reason === "pending_horizon"`, DEC-2D-2 honest abstention).
// See veridex/runtime/orchestrator.py finalize() and veridex/runtime/window.py.
function clvFromLawResultPayload(payload: Record<string, unknown>): CanonicalEvent['clv'] | undefined {
  if (payload.reason === 'pending_horizon' || payload.clv_bps === 'pending') return { kind: 'pending' };
  if (typeof payload.window_clv_bps === 'number') return { kind: 'window_clv', bps: payload.window_clv_bps };
  if (typeof payload.clv_bps === 'number') return { kind: 'clv', bps: payload.clv_bps };
  return undefined;
}

function policyFromPayload(payload: Record<string, unknown>, seq: number): PolicyDecision {
  const reasonCodes = Array.isArray(payload.reason_codes) ? (payload.reason_codes as string[]) : null;
  return {
    tick_seq: typeof payload.tick_seq === 'number' ? payload.tick_seq : seq,
    decision: (payload.decision as PolicyDecision['decision']) ?? 'DENY',
    reason: typeof payload.reason === 'string' ? payload.reason : reasonCodes ? reasonCodes.join(', ') : '',
    edge_bps: typeof payload.edge_bps === 'number' ? payload.edge_bps : undefined,
    min_edge_bps: typeof payload.min_edge_bps === 'number' ? payload.min_edge_bps : undefined,
  };
}

// Map ONE raw WS frame (the real backend wire shape OR the simplified {type,ts} fixture shape) to
// the frontend CanonicalEvent view-model, deriving the honest CLV cell + PolicyDecision the Cockpit
// needs to render live (T10). Exported for direct unit testing.
export function normalizeWireEvent(raw: WireEvent): CanonicalEvent {
  const rawType = raw.event_type ?? raw.type ?? '';
  const payload = raw.payload ?? {};
  const event: CanonicalEvent = {
    seq: raw.seq,
    type: TYPE_ALIASES[rawType] ?? rawType,
    payload_hash: raw.payload_hash,
    evidence: raw.evidence,
    ts: raw.event_ts ?? raw.ts ?? 0,
    agent_id: raw.agent_id ?? (payload.agent_id as string | undefined),
    summary: raw.summary ?? deriveSummary(rawType, payload),
  };
  if (rawType === 'law_result') {
    const clv = clvFromLawResultPayload(payload);
    if (clv) event.clv = clv;
  }
  if (rawType === 'policy_result') {
    event.policy = policyFromPayload(payload, raw.seq);
  }
  return event;
}

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
    try { event = normalizeWireEvent(JSON.parse(raw) as WireEvent); } catch { return; }

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
