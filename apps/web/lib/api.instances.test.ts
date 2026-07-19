// F-3: owner-scoped deployed-instance readers (I-2). getInstances/getInstance bind the FROZEN
// AgentInstance contract (veridex/deploy/instance.py) and attach the auth-contract@1 bearer via
// the SAME injectable seam (lib/auth.ts) as deployAgent — an authed GET, fail-closed on no token
// (never fabricates one). Mock-gated exactly like every other reader.
//
// These tests use a FAKE injected token provider + a stubbed fetch — no network, no real Privy SDK.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  getInstances, getInstance, adaptAgentInstance,
  type AgentInstanceWire,
} from '@/lib/api';
import { setAuthTokenProvider, resetAuthTokenProvider } from '@/lib/auth';

function wireInstance(overrides: Partial<AgentInstanceWire> = {}): AgentInstanceWire {
  return {
    instance_id: 'inst_abc',
    template_id: 'value_clv',
    agent_id: 'studio-value_clv',
    config_hash: 'c'.repeat(64),
    policy_hash: 'p'.repeat(64),
    source_mode: 'replay',
    execution_mode: 'paper',
    market_allowlist: ['moneyline'],
    venue_allowlist: ['polymarket'],
    run_id: 'run_evidence_01',
    status: 'running',
    last_failure_reason: null,
    operator_id: 'did:privy:owner-1',
    runtime_handle: { runtime_kind: 'agentos', runtime_agent_id: 'aos_1', session_id: 'sess_replaceable', run_id: 'run_evidence_01' },
    created_at: '2026-07-17T00:00:00Z',
    updated_at: '2026-07-17T00:00:00Z',
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

describe('adaptAgentInstance — faithful wire → view-model mapping (honesty: never coerced)', () => {
  it('preserves run_id (authoritative evidence identity) and status verbatim; keeps session_id as the replaceable handle', () => {
    const v = adaptAgentInstance(wireInstance({ status: 'sealed' }));
    expect(v.run_id).toBe('run_evidence_01');
    expect(v.status).toBe('sealed'); // preserved verbatim, never mapped to a rosier state
    expect(v.runtime_handle?.session_id).toBe('sess_replaceable');
    expect(v.runtime_handle?.run_id).toBe('run_evidence_01');
    expect(v.operator_id).toBe('did:privy:owner-1');
    expect(v.source_mode).toBe('replay');
  });

  it('carries a null runtime_handle through as null (no fabricated session)', () => {
    const v = adaptAgentInstance(wireInstance({ runtime_handle: null }));
    expect(v.runtime_handle).toBeNull();
  });
});

describe('getInstances / getInstance — owner-scoped authed GET (auth-contract@1 bearer)', () => {
  it('getInstances attaches Authorization: Bearer from the injected seam and GETs /agents/instances', async () => {
    setAuthTokenProvider(async () => 'owner-token-xyz');
    const fetchMock = vi.fn(async () => new Response(JSON.stringify([wireInstance()]), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const list = await getInstances();

    expect(list).toHaveLength(1);
    expect(list[0].instance_id).toBe('inst_abc');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(url)).toMatch(/\/agents\/instances$/);
    expect((init.method ?? 'GET')).toBe('GET');
    expect(new Headers(init.headers).get('authorization')).toBe('Bearer owner-token-xyz');
  });

  it('getInstances with NO token fires the GET WITHOUT an Authorization header (never fabricates a bearer — fails closed at the backend)', async () => {
    setAuthTokenProvider(async () => null);
    const fetchMock = vi.fn(async () => new Response(JSON.stringify([]), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    await getInstances();

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(new Headers(init.headers).has('authorization')).toBe(false);
  });

  it('getInstance surfaces a 403 (owned by another principal) as ApiError(403) — never a fabricated instance', async () => {
    setAuthTokenProvider(async () => 'owner-token-xyz');
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'not yours' }), { status: 403 })));
    await expect(getInstance('inst_not_mine')).rejects.toMatchObject({ status: 403 });
  });

  it('getInstance surfaces a 404 (absent / unowned legacy row) as ApiError(404)', async () => {
    setAuthTokenProvider(async () => 'owner-token-xyz');
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'not found' }), { status: 404 })));
    await expect(getInstance('inst_ghost')).rejects.toMatchObject({ status: 404 });
  });

  it('getInstance GETs the per-instance path with the bearer', async () => {
    setAuthTokenProvider(async () => 'owner-token-xyz');
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(wireInstance({ instance_id: 'inst_mine' })), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const inst = await getInstance('inst_mine');

    expect(inst.instance_id).toBe('inst_mine');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(url)).toMatch(/\/agents\/instances\/inst_mine$/);
    expect(new Headers(init.headers).get('authorization')).toBe('Bearer owner-token-xyz');
  });
});

describe('getInstances — mock gate (follows the existing mock-vs-live gate exactly)', () => {
  it('mock ON: short-circuits to DEMO instances WITHOUT calling fetch (replay, never a live backend hit)', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    const list = await getInstances();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(list.length).toBeGreaterThanOrEqual(1);
    // DEMO data is REPLAY, never rendered under a LIVE badge (doctrine).
    expect(list.every((i) => i.source_mode === 'replay')).toBe(true);
  });

  it('mock ON: a KNOWN demo instance id resolves to that instance', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    const inst = await getInstance('inst_demo_value_clv');
    expect(inst.instance_id).toBe('inst_demo_value_clv');
    expect(inst.source_mode).toBe('replay');
  });

  it('mock ON: an UNKNOWN instance id throws ApiError(404) — never fabricates the first demo instance', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    await expect(getInstance('inst_does_not_exist')).rejects.toMatchObject({ status: 404 });
  });
});
