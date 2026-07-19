import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import {
  useAgentOps, useRuntimeEvents, deriveOverview, eventToLogLine, formatOpsTs,
} from '@/components/ops/useAgentOps';
import type { RuntimeEventRecord } from '@/lib/api';

// The hook resolves the open agent → the owner's matching instances (F-3 seam) and polls each
// instance's runtime-events. Both readers are mocked here — no network, no real Privy SDK.
const getInstances = vi.fn();
const getRuntimeEvents = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  getInstances: (...a: unknown[]) => getInstances(...a),
  getRuntimeEvents: (...a: unknown[]) => getRuntimeEvents(...a),
}));

const isMockEnabled = vi.fn(() => false);
vi.mock('@/lib/mock', async (orig) => ({
  ...(await orig<typeof import('@/lib/mock')>()),
  isMockEnabled: () => isMockEnabled(),
}));

function ev(overrides: Partial<RuntimeEventRecord> = {}): RuntimeEventRecord {
  return {
    id: 1, type: 'action_emitted', agent_id: 'studio-momentum', run_id: 'run_1',
    session_id: 'sess_1', ts: 1782518393000, channel: 'OPS', payload: { action: 'FOLLOW_MOMENTUM' },
    ...overrides,
  };
}
function instance(instanceId: string, agentId: string) {
  return { instance_id: instanceId, agent_id: agentId };
}

beforeEach(() => {
  vi.clearAllMocks();
  isMockEnabled.mockReturnValue(false);
  getInstances.mockResolvedValue([]);
  getRuntimeEvents.mockResolvedValue([]);
});
afterEach(() => { vi.useRealTimers(); });

describe('useAgentOps (open/close state — unchanged)', () => {
  it('opens for an agent and closes', () => {
    const { result } = renderHook(() => useAgentOps());
    expect(result.current.isOpen).toBe(false);
    act(() => result.current.open('momentum_fr'));
    expect(result.current.isOpen).toBe(true);
    expect(result.current.agentId).toBe('momentum_fr');
    act(() => result.current.close());
    expect(result.current.isOpen).toBe(false);
  });
});

describe('deriveOverview — faithful projection from the OPS event stream (never a fixture)', () => {
  it('returns null for an empty stream (honest "no runtime data")', () => {
    expect(deriveOverview('a', [])).toBeNull();
  });

  it('takes status from the latest STATUS_CHANGED and counts errors/retries/tool_calls by type', () => {
    const o = deriveOverview('a', [
      ev({ id: 1, type: 'status_changed', payload: { status: 'running' } }),
      ev({ id: 2, type: 'tool_call' }),
      ev({ id: 3, type: 'error', payload: { error: 'boom' } }),
      ev({ id: 4, type: 'retry' }),
      ev({ id: 5, type: 'status_changed', payload: { status: 'failed' } }),
    ])!;
    expect(o.status).toBe('failed'); // latest status wins
    expect(o.errors).toBe(1);
    expect(o.retries).toBe(1);
    expect(o.tool_calls).toBe(1);
  });

  it('binds latency/last_action/schema_valid from their payloads; leaves absent optional fields null ("—")', () => {
    const o = deriveOverview('a', [
      ev({ id: 1, type: 'latency', payload: { latency_ms: 412 } }),
      ev({ id: 2, type: 'action_emitted', payload: { action: 'FOLLOW_MOMENTUM' } }),
      ev({ id: 3, type: 'schema_validation', payload: { valid: true } }),
    ])!;
    expect(o.latest_model_latency_ms).toBe(412);
    expect(o.last_action).toBe('FOLLOW_MOMENTUM');
    expect(o.schema_valid).toBe(true);
    expect(o.latest_model_tokens).toBeNull(); // no token_usage emitted → honest "—", never a fabricated 0
  });
});

describe('eventToLogLine — OPS-channel log projection (SEC-003: telemetry, never proof)', () => {
  it('maps an OPS event to a canonical log line (channel OPS; event = type; detail from payload)', () => {
    const line = eventToLogLine(ev({ type: 'action_emitted', payload: { action: 'FADE' } }));
    expect(line.channel).toBe('OPS'); // runtime events are ALWAYS OPS (never PROOF/POLICY/EXEC)
    expect(line.event).toBe('action_emitted');
    expect(line.detail).toContain('FADE');
  });

  it('formats a ms timestamp to HH:MM:SS.mmm', () => {
    expect(formatOpsTs(0)).toMatch(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
  });
});

describe('useRuntimeEvents — mock gate (demo behind isMockEnabled, never in the live path)', () => {
  it('mock ON: serves DEMO overview + log WITHOUT any network call', async () => {
    isMockEnabled.mockReturnValue(true);
    const { result } = renderHook(() => useRuntimeEvents('momentum_fr'));
    await act(async () => { await Promise.resolve(); });
    expect(getInstances).not.toHaveBeenCalled();
    expect(getRuntimeEvents).not.toHaveBeenCalled();
    expect(result.current.isDemo).toBe(true);
    expect(result.current.overview).not.toBeNull();
    expect(result.current.log.length).toBeGreaterThan(0);
  });
});

describe('useRuntimeEvents — live cursor-polling (RED controls #2–#5)', () => {
  it('#2 advances the since-cursor across polls and never duplicates events', async () => {
    vi.useFakeTimers();
    getInstances.mockResolvedValue([instance('inst_1', 'a')]);
    getRuntimeEvents.mockImplementation(async (_id: string, since: number) => {
      if (since === 0) return [ev({ id: 1 }), ev({ id: 2 })];
      if (since === 2) return [ev({ id: 3 })];
      return [];
    });

    const { result } = renderHook(() => useRuntimeEvents('a'));
    await act(async () => { await vi.advanceTimersByTimeAsync(1); }); // initial poll
    expect(getRuntimeEvents).toHaveBeenLastCalledWith('inst_1', 0);
    expect(result.current.log).toHaveLength(2);

    await act(async () => { await vi.advanceTimersByTimeAsync(3000); }); // next poll
    expect(getRuntimeEvents).toHaveBeenLastCalledWith('inst_1', 2); // cursor advanced to max(id)
    expect(result.current.log).toHaveLength(3); // 3 unique events, no duplicate of ids 1/2
  });

  it('#3 stops polling after unmount (no further fetch)', async () => {
    vi.useFakeTimers();
    getInstances.mockResolvedValue([instance('inst_1', 'a')]);
    getRuntimeEvents.mockResolvedValue([ev({ id: 1 })]);

    const { unmount } = renderHook(() => useRuntimeEvents('a'));
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    const callsBefore = getRuntimeEvents.mock.calls.length;

    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(9000); });
    expect(getRuntimeEvents.mock.calls.length).toBe(callsBefore); // no fetch after unmount
  });

  it('#4 resets the cursor + clears events when the instance (agent) switches — no stale carry-over', async () => {
    vi.useFakeTimers();
    getInstances.mockResolvedValue([instance('inst_1', 'a'), instance('inst_2', 'b')]);
    getRuntimeEvents.mockImplementation(async (id: string, since: number) => {
      if (id === 'inst_1' && since === 0) return [ev({ id: 5, agent_id: 'a', payload: { action: 'WAIT' } })];
      if (id === 'inst_2' && since === 0) return [ev({ id: 9, agent_id: 'b', payload: { action: 'FADE' } })];
      return [];
    });

    const { result, rerender } = renderHook(({ a }) => useRuntimeEvents(a), { initialProps: { a: 'a' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(result.current.log.some((l) => l.detail.includes('WAIT'))).toBe(true);

    rerender({ a: 'b' });
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    // fresh instance polled from since=0 (cursor reset), and the prior agent's events are gone
    expect(getRuntimeEvents).toHaveBeenLastCalledWith('inst_2', 0);
    expect(result.current.log.some((l) => l.detail.includes('WAIT'))).toBe(false);
    expect(result.current.log.some((l) => l.detail.includes('FADE'))).toBe(true);
  });

  it('MAJOR-1: two instances sharing one agent_id do NOT double events or Overview counters (dedup by durable id)', async () => {
    vi.useFakeTimers();
    // Studio deploys the same archetype with a template-constant agent_id → 2 instances, one agent_id.
    getInstances.mockResolvedValue([instance('inst_1', 'a'), instance('inst_2', 'a')]);
    // The backend keys runtime-events by agent_id (store filters WHERE agent_id=%s), so BOTH
    // instances return the IDENTICAL durable-id list — the events must be counted exactly once.
    const shared = [
      ev({ id: 1, type: 'error', payload: { error: 'boom' } }),
      ev({ id: 2, type: 'action_emitted', payload: { action: 'WAIT' } }),
    ];
    getRuntimeEvents.mockImplementation(async (_id: string, since: number) => (since === 0 ? shared : []));

    const { result } = renderHook(() => useRuntimeEvents('a'));
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });

    expect(result.current.log).toHaveLength(2); // NOT 4 — no duplicate of ids 1/2 across instances
    expect(result.current.overview?.errors).toBe(1); // NOT 2 — counters not doubled
  });

  it('MINOR-1: an instance switch clears the prior agent events with no stale carry-over frame', async () => {
    vi.useFakeTimers();
    getInstances.mockResolvedValue([instance('inst_1', 'a'), instance('inst_2', 'b')]);
    getRuntimeEvents.mockImplementation(async (id: string, since: number) => {
      if (id === 'inst_1' && since === 0) return [ev({ id: 5, agent_id: 'a', payload: { action: 'WAIT' } })];
      if (id === 'inst_2' && since === 0) return [ev({ id: 9, agent_id: 'b', payload: { action: 'FADE' } })];
      return [];
    });

    const { result, rerender } = renderHook(({ a }) => useRuntimeEvents(a), { initialProps: { a: 'a' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(result.current.log.some((l) => l.detail.includes('WAIT'))).toBe(true);

    // On switch, the prior agent's events are cleared synchronously (render-phase reset) — the very
    // next observed state must NOT still be showing agent a's events under agent b.
    rerender({ a: 'b' });
    expect(result.current.log.some((l) => l.detail.includes('WAIT'))).toBe(false);
    expect(result.current.overview).toBeNull(); // no derived overview from stale carry-over
  });

  it('#5 mock OFF + no owned events → honest-empty (overview null, empty log), NEVER a fixture', async () => {
    getInstances.mockResolvedValue([]); // caller owns no instance for this agent
    const { result } = renderHook(() => useRuntimeEvents('a'));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(result.current.overview).toBeNull();
    expect(result.current.log).toEqual([]);
    expect(result.current.isDemo).toBe(false);
    expect(getRuntimeEvents).not.toHaveBeenCalled();
  });

  it('surfaces a reader failure as an honest error (never a fixture fallback — T-2)', async () => {
    getInstances.mockRejectedValue(new Error('403 forbidden'));
    const { result } = renderHook(() => useRuntimeEvents('a'));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(result.current.error).toBeTruthy();
    expect(result.current.overview).toBeNull();
  });
});
