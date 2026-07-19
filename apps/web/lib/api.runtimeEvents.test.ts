// F-6: owner-scoped runtime-events reader (I-4). getRuntimeEvents binds the FROZEN
// RuntimeEventsResponse contract (veridex/api/router.py:1596 → RuntimeEvent + durable `id` cursor)
// and attaches the auth-contract@1 bearer via the SAME injectable seam (lib/auth.ts) as
// getInstances — an authed GET, fail-closed on no token, THROWS on non-ok (no fixture fallback, T-2).
//
// These tests use a FAKE injected token provider + a stubbed fetch — no network, no real Privy SDK.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { getRuntimeEvents, PATHS, ApiError, type RuntimeEventRecord } from '@/lib/api';
import { setAuthTokenProvider, resetAuthTokenProvider } from '@/lib/auth';

function opsEvent(overrides: Partial<RuntimeEventRecord> = {}): RuntimeEventRecord {
  return {
    id: 1,
    type: 'action_emitted',
    agent_id: 'studio-momentum',
    run_id: 'run_evidence_01',
    session_id: 'sess_1',
    ts: 1782518393000,
    channel: 'OPS',
    payload: { action: 'FOLLOW_MOMENTUM' },
    ...overrides,
  };
}

beforeEach(() => { vi.restoreAllMocks(); });
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
  resetAuthTokenProvider();
});

describe('PATHS.instanceRuntimeEvents — owner-scoped instance route with cursor (retires the public path)', () => {
  it('builds the owner-scoped instance path with the since cursor', () => {
    expect(PATHS.instanceRuntimeEvents('inst_abc', 0)).toBe('/agents/instances/inst_abc/runtime-events?since=0');
    expect(PATHS.instanceRuntimeEvents('inst_abc', 42)).toBe('/agents/instances/inst_abc/runtime-events?since=42');
  });

  it('appends the forward-paging limit when set', () => {
    expect(PATHS.instanceRuntimeEvents('inst_abc', 5, 100)).toBe(
      '/agents/instances/inst_abc/runtime-events?since=5&limit=100',
    );
  });

  it('has RETIRED the orphaned public /agents/{id}/runtime-events path', () => {
    expect((PATHS as Record<string, unknown>).runtimeEvents).toBeUndefined();
  });
});

describe('getRuntimeEvents — owner-scoped authed GET, cursor-polling, honest on failure', () => {
  it('attaches the bearer and GETs the owner-scoped instance path with the since cursor', async () => {
    setAuthTokenProvider(async () => 'owner-token-xyz');
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ events: [opsEvent()] }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const events = await getRuntimeEvents('inst_abc', 7);

    expect(events).toHaveLength(1);
    expect(events[0].id).toBe(1);
    expect(events[0].channel).toBe('OPS');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(url)).toMatch(/\/agents\/instances\/inst_abc\/runtime-events\?since=7$/);
    expect((init.method ?? 'GET')).toBe('GET');
    expect(new Headers(init.headers).get('authorization')).toBe('Bearer owner-token-xyz');
  });

  it('unwraps the single-field { events } envelope (mirrors LeaderboardResponse{rows})', async () => {
    setAuthTokenProvider(async () => 't');
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ events: [opsEvent({ id: 3 }), opsEvent({ id: 4 })] }), { status: 200 },
    )));
    const events = await getRuntimeEvents('inst_abc', 2);
    expect(events.map((e) => e.id)).toEqual([3, 4]);
  });

  it('with NO token fires the GET WITHOUT an Authorization header (never fabricates a bearer)', async () => {
    setAuthTokenProvider(async () => null);
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ events: [] }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await getRuntimeEvents('inst_abc', 0);
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(new Headers(init.headers).has('authorization')).toBe(false);
  });

  it('THROWS ApiError(403) on wrong-owner — never falls back to a fixture (T-2 honesty)', async () => {
    setAuthTokenProvider(async () => 'owner-token-xyz');
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'not yours' }), { status: 403 })));
    await expect(getRuntimeEvents('inst_not_mine', 0)).rejects.toBeInstanceOf(ApiError);
    await expect(getRuntimeEvents('inst_not_mine', 0)).rejects.toMatchObject({ status: 403 });
  });

  it('THROWS ApiError(404) on absent/unowned instance (no existence leak, no fixture)', async () => {
    setAuthTokenProvider(async () => 'owner-token-xyz');
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'not found' }), { status: 404 })));
    await expect(getRuntimeEvents('inst_ghost', 0)).rejects.toMatchObject({ status: 404 });
  });
});
