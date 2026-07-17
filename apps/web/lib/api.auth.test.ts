// auth-contract@1 (frontend half, I-1b): deployAgent (and any future owner-scoped call) binds
// the FROZEN auth contract — Authorization: Bearer <privy_access_token>, and a 401 triggers
// re-auth (re-acquire token) then a single retry, never a silent drop.
//
// Fail-closed ("no token → UI shows login, never fires") is enforced at the UI layer
// (components/auth/AuthGate.test.tsx — the gated affordance is never rendered without a
// session) plus the backend (I-1: 401 before any side effect). This client-transport layer is a
// dumb, honest relay: it attaches the bearer when the seam has one and never fabricates one when
// it doesn't — see the "no token available" test below.
//
// These tests use a FAKE injected token provider (setAuthTokenProvider) — no network, no real
// Privy SDK. The seam is production code (lib/auth.ts); tests only ever inject a fake function.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { deployAgent, type DeployAgentPayload } from '@/lib/api';
import { setAuthTokenProvider, resetAuthTokenProvider } from '@/lib/auth';

const PAYLOAD: DeployAgentPayload = {
  template_id: 'value_clv',
  agent_id: 'studio-value_clv',
  strategy: 'baseline',
  source_mode: 'replay',
  execution_mode: 'paper',
  market_allowlist: ['moneyline'],
  venue_allowlist: ['polymarket'],
  min_edge_bps: 8,
  max_stake: 100,
  window_id: 'studio-value_clv',
  fixture_id: 1,
  end_rule: 'pre_match',
};

function okDeploy() {
  return new Response(
    JSON.stringify({ instance_id: 'inst_1', config_hash: 'a'.repeat(64), policy_hash: 'b'.repeat(64), run_id: 'run_1' }),
    { status: 200 },
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetAuthTokenProvider(); // never leak a fake provider into another test file
});

describe('deployAgent — auth-contract@1 bearer attachment (frontend half of I-1)', () => {
  it('attaches Authorization: Bearer <token> from the injected token provider', async () => {
    setAuthTokenProvider(async () => 'fake-access-token-123');
    const fetchMock = vi.fn(async () => okDeploy());
    vi.stubGlobal('fetch', fetchMock);

    await deployAgent(PAYLOAD);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/agents\/deploy$/);
    const headers = new Headers(init.headers);
    expect(headers.get('authorization')).toBe('Bearer fake-access-token-123');
  });

  it('no token in the provider: fires the deploy fetch WITHOUT an Authorization header (never fabricates a bearer)', async () => {
    setAuthTokenProvider(async () => null); // no session — matches the seam's fail-closed default
    const fetchMock = vi.fn(async () => okDeploy());
    vi.stubGlobal('fetch', fetchMock);

    await deployAgent(PAYLOAD);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(new Headers(init.headers).has('authorization')).toBe(false);
  });
});

describe('deployAgent — 401 triggers re-auth then a single retry (auth-contract@1)', () => {
  it('on 401, re-acquires the token from the provider and retries once, succeeding with the new token', async () => {
    let calls = 0;
    setAuthTokenProvider(async () => {
      calls += 1;
      // First acquisition returns a stale token; re-auth (second call, triggered by the 401)
      // returns a fresh one.
      return calls === 1 ? 'stale-token' : 'fresh-token';
    });
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      if (headers.get('authorization') === 'Bearer stale-token') {
        return new Response(JSON.stringify({ detail: 'expired' }), { status: 401 });
      }
      return okDeploy();
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await deployAgent(PAYLOAD);

    expect(result.run_id).toBe('run_1');
    expect(fetchMock).toHaveBeenCalledTimes(2); // exactly one retry — never an infinite loop
    const secondInit = fetchMock.mock.calls[1][1] as RequestInit;
    expect(new Headers(secondInit.headers).get('authorization')).toBe('Bearer fresh-token');
  });

  it('a 401 that persists after re-auth surfaces as ApiError(401) — never silently dropped', async () => {
    setAuthTokenProvider(async () => 'always-expired-token');
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ detail: 'expired' }), { status: 401 }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(deployAgent(PAYLOAD)).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).toHaveBeenCalledTimes(2); // one original + exactly one retry, then it gives up
  });

  it('re-auth producing no token (truly logged out) still surfaces the 401 — no retry fetch, no throw swallowed', async () => {
    let calls = 0;
    setAuthTokenProvider(async () => {
      calls += 1;
      return calls === 1 ? 'stale-token' : null; // re-login attempt failed — logged out
    });
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ detail: 'expired' }), { status: 401 }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(deployAgent(PAYLOAD)).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).toHaveBeenCalledTimes(1); // no retry fired without a token to retry with
  });
});
