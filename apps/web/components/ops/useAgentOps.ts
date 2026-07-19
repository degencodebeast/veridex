'use client';
import { useEffect, useState } from 'react';
import { getInstances, getRuntimeEvents, type RuntimeEventRecord } from '@/lib/api';
import { isMockEnabled } from '@/lib/mock';
import { RUNTIME_OVERVIEW, RUNTIME_LOG } from '@/lib/fixtures/catalog';
import type { CanonicalLogLine, RuntimeOverview, RuntimeStatus, SportsActionType } from '@/lib/catalog';

// ---- open/close drawer state (unchanged) ------------------------------------
export interface AgentOpsState {
  isOpen: boolean;
  agentId: string | null;
  open: (id: string) => void;
  close: () => void;
}

export function useAgentOps(): AgentOpsState {
  const [agentId, setAgentId] = useState<string | null>(null);
  return {
    isOpen: agentId !== null,
    agentId,
    open: (id: string) => setAgentId(id),
    close: () => setAgentId(null),
  };
}

// ---- runtime-events data path (F-6) -----------------------------------------
//
// The Agent Ops drawer's DURABLE runtime feed. The endpoint is OWNER-SCOPED by INSTANCE
// (GET /agents/instances/{id}/runtime-events), but the drawer opens by AGENT id — so the honest
// bridge is: resolve the caller's own instances (the F-3 getInstances seam), keep only those for the
// open agent, and cursor-poll each ("both runtimes if present"). All runtime events are OPS-channel
// telemetry (SEC-003): never proof, never scored. The LIVE path throws on non-ok and NEVER falls
// back to a fixture (T-2) — demo data is served ONLY behind isMockEnabled().

const POLL_INTERVAL_MS = 3000;
const RUNTIME_STATUSES: readonly RuntimeStatus[] = ['running', 'paused', 'failed', 'completed'];

export interface RuntimeOpsData {
  overview: RuntimeOverview | null;
  log: CanonicalLogLine[];
  loading: boolean;
  error: string | null;
  isDemo: boolean;
}

/** Format a wall-clock ms timestamp as HH:MM:SS.mmm (UTC — stable across the poller's re-renders). */
export function formatOpsTs(ms: number): string {
  const d = new Date(ms);
  const p = (n: number, w = 2) => String(n).padStart(w, '0');
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}.${p(d.getUTCMilliseconds(), 3)}`;
}

// Render one payload as a concise, secret-free detail string. Prefer an explicit `summary`, then the
// well-known typed keys the backend emits (action/status/error/latency_ms/valid), else compact JSON.
function payloadDetail(payload: Record<string, unknown>): string {
  if (typeof payload.summary === 'string') return payload.summary;
  const parts: string[] = [];
  if (typeof payload.action === 'string') parts.push(String(payload.action));
  if (typeof payload.status === 'string') parts.push(String(payload.status));
  if (typeof payload.latency_ms === 'number') parts.push(`${payload.latency_ms}ms`);
  if (typeof payload.valid === 'boolean') parts.push(`valid=${payload.valid}`);
  if (typeof payload.error === 'string') parts.push(String(payload.error));
  if (parts.length) return parts.join(' · ');
  return Object.keys(payload).length ? JSON.stringify(payload) : '';
}

/** Project one served OPS RuntimeEvent → a Logs-tab line. Channel is always OPS (never a proof line). */
export function eventToLogLine(e: RuntimeEventRecord): CanonicalLogLine {
  return { ts: formatOpsTs(e.ts), channel: e.channel, event: e.type, detail: payloadDetail(e.payload) };
}

/**
 * Derive the Overview tab from the OPS event stream. Returns null for an empty stream so the drawer
 * shows an honest "no runtime data" — never a fixture. Optional-tier fields (latency, tokens,
 * last_action, schema_valid) stay null when the runtime never emitted them (rendered "—"), never a
 * fabricated 0/true. Counts (errors/retries/tool_calls) come straight from the event-type tally.
 */
export function deriveOverview(agentId: string, events: RuntimeEventRecord[]): RuntimeOverview | null {
  if (events.length === 0) return null;
  let status: RuntimeStatus = 'running';
  let runId: string | null = null;
  let latencyMs: number | null = null;
  let tokens: number | null = null;
  let lastAction: SportsActionType | null = null;
  let schemaValid: boolean | null = null;
  let errors = 0;
  let retries = 0;
  let toolCalls = 0;

  for (const e of events) {
    if (e.run_id) runId = e.run_id;
    const p = e.payload;
    switch (e.type) {
      case 'status_changed':
        if (typeof p.status === 'string' && (RUNTIME_STATUSES as string[]).includes(p.status)) {
          status = p.status as RuntimeStatus;
        }
        break;
      case 'run_failed': status = 'failed'; break;
      case 'run_completed': status = 'completed'; break;
      case 'action_emitted':
        if (typeof p.action === 'string') lastAction = p.action as SportsActionType;
        break;
      case 'latency':
        if (typeof p.latency_ms === 'number') latencyMs = p.latency_ms;
        break;
      case 'token_usage':
        if (typeof p.tokens === 'number') tokens = p.tokens;
        else if (typeof p.total_tokens === 'number') tokens = p.total_tokens;
        break;
      case 'schema_validation':
        if (typeof p.valid === 'boolean') schemaValid = p.valid;
        break;
      case 'error': errors += 1; break;
      case 'retry': retries += 1; break;
      case 'tool_call': toolCalls += 1; break;
      default: break;
    }
  }

  return {
    agent_id: agentId,
    run_id: runId,
    status,
    latest_model_latency_ms: latencyMs,
    latest_model_tokens: tokens,
    last_action: lastAction,
    schema_valid: schemaValid,
    errors,
    retries,
    tool_calls: toolCalls,
    // `source` is not carried on the wire RuntimeEvent and is not rendered by the drawer; the honest
    // owner-scoped identity (STUDIO vs BYOA) lives on the instance record, not the telemetry stream.
    source: 'STUDIO',
  };
}

/**
 * Poll the caller's OWN durable runtime-events for the open agent.
 *
 * Live (mock OFF): resolve the owner's instances, keep those for `agentId`, and cursor-poll each on
 * a {@link POLL_INTERVAL_MS} cadence — `since` advances to the max durable `id` (exclusive cursor ⇒
 * no duplicates). The poll is torn down on unmount AND reset (events cleared, cursors back to 0) when
 * `agentId` switches, so no stale carry-over survives an instance switch. Any reader failure surfaces
 * as `error` (honest) — it never falls back to a fixture.
 *
 * Demo (mock ON): serves the DEMO overview/log WITHOUT any network call, flagged `isDemo`.
 */
export function useRuntimeEvents(agentId: string | null): RuntimeOpsData {
  const demo = isMockEnabled();
  const [events, setEvents] = useState<RuntimeEventRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Render-phase reset (React's "adjust state on prop change" pattern): when the open agent switches,
  // clear the prior agent's events/error IN THE SAME render — so no stale carry-over frame is ever
  // committed before the effect below re-runs. Guarded by a tracked value so it fires once per switch.
  const [trackedAgent, setTrackedAgent] = useState(agentId);
  if (agentId !== trackedAgent) {
    setTrackedAgent(agentId);
    setEvents([]);
    setError(null);
  }

  useEffect(() => {
    if (agentId == null || demo) return undefined;

    let cancelled = false;
    const cursors = new Map<string, number>();
    let instanceIds: string[] | null = null;
    // Accumulate by durable `id` (BIGSERIAL, globally unique). A single agent can have ≥2 deployed
    // instances that share ONE agent_id (Studio deploys with a template-constant `studio-${archetype}`
    // id), and the backend serves runtime-events keyed by agent_id — so both instances return the
    // IDENTICAL list. Keying by id dedupes those, so multi-instance agents never double events (nor
    // the Overview counters derived from them). "Dedup by construction" holds ACROSS instances here.
    const byId = new Map<number, RuntimeEventRecord>();

    setLoading(true);

    const poll = async () => {
      try {
        if (instanceIds === null) {
          const instances = await getInstances();
          if (cancelled) return;
          instanceIds = instances.filter((i) => i.agent_id === agentId).map((i) => i.instance_id);
          for (const id of instanceIds) cursors.set(id, 0);
        }
        let changed = false;
        for (const id of instanceIds) {
          const since = cursors.get(id) ?? 0;
          const batch = await getRuntimeEvents(id, since);
          if (cancelled) return;
          if (batch.length > 0) {
            cursors.set(id, batch.reduce((m, e) => (e.id > m ? e.id : m), since));
            for (const e of batch) byId.set(e.id, e);
            changed = true;
          }
        }
        if (changed) {
          setEvents([...byId.values()].sort((a, b) => (a.ts - b.ts) || (a.id - b.id)));
        }
        if (!cancelled) { setLoading(false); setError(null); }
      } catch (e) {
        if (!cancelled) { setLoading(false); setError(e instanceof Error ? e.message : String(e)); }
      }
    };

    void poll();
    const timer = setInterval(() => { void poll(); }, POLL_INTERVAL_MS);
    return () => { cancelled = true; clearInterval(timer); };
  }, [agentId, demo]);

  if (demo && agentId != null) {
    return {
      overview: RUNTIME_OVERVIEW[agentId] ?? null,
      log: RUNTIME_LOG,
      loading: false,
      error: null,
      isDemo: true,
    };
  }

  return {
    overview: agentId == null ? null : deriveOverview(agentId, events),
    log: events.map(eventToLogLine),
    loading,
    error,
    isDemo: false,
  };
}
