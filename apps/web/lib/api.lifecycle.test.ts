// F-7: owner-scoped instance lifecycle (status / kill) + competition kill-switch (disable execution).
// These bind the FROZEN backend contracts field-for-field:
//   - GET  /agents/instances/{id}/status  → InstanceStatusResponse (deploy.py:985)
//   - POST /agents/instances/{id}/kill     → KillResponse           (deploy.py:1008)
//   - POST /competitions/{id}/kill-switch  → KillSwitchResponse     (router.py:1654)
// Each attaches the auth-contract@1 bearer via the SAME injectable seam (lib/auth.ts) as deployAgent;
// no token → fires WITHOUT an Authorization header (never fabricates a bearer — fails closed at the
// backend). NO pauseInstance exists: the runtime has no pause/resume endpoint (deploy.py CON-2D-701).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  getInstanceStatus, killInstance, armCompetitionKillSwitch,
  ApiError,
  type InstanceStatus, type KillResult, type KillSwitchResult,
} from '@/lib/api';
import { setAuthTokenProvider, resetAuthTokenProvider } from '@/lib/auth';

function statusWire(overrides: Partial<InstanceStatus> = {}): InstanceStatus {
  return {
    instance_id: 'inst_abc',
    run_id: 'run_evidence_01',
    run_state: 'running',
    killed: false,
    status: 'running',
    lease_status: 'active',
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

describe('getInstanceStatus — owner-scoped run/lease status (auth-contract@1)', () => {
  it('GETs /agents/instances/{id}/status with the bearer and maps the wire fields verbatim', async () => {
    setAuthTokenProvider(async () => 'owner-token-xyz');
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(statusWire({ run_state: 'cancelled', killed: true })), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const s = await getInstanceStatus('inst_abc');

    expect(s.run_state).toBe('cancelled'); // preserved verbatim — never coerced to a rosier state
    expect(s.killed).toBe(true);
    expect(s.run_id).toBe('run_evidence_01');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(url)).toMatch(/\/agents\/instances\/inst_abc\/status$/);
    expect((init.method ?? 'GET')).toBe('GET');
    expect(new Headers(init.headers).get('authorization')).toBe('Bearer owner-token-xyz');
  });

  it('with NO token fires WITHOUT an Authorization header (never fabricates a bearer)', async () => {
    setAuthTokenProvider(async () => null);
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(statusWire()), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await getInstanceStatus('inst_abc');
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(new Headers(init.headers).has('authorization')).toBe(false);
  });

  it('surfaces a 403 (owned by another principal) as ApiError(403) — never a fabricated status', async () => {
    setAuthTokenProvider(async () => 'owner-token-xyz');
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 403 })));
    await expect(getInstanceStatus('inst_not_mine')).rejects.toMatchObject({ status: 403 });
  });
});

describe('killInstance — owner-gated exactly-once kill (II-6 / AC-16)', () => {
  it('POSTs /agents/instances/{id}/kill with the bearer and returns the KillResponse (engaged + phase)', async () => {
    setAuthTokenProvider(async () => 'owner-token-xyz');
    const body: KillResult = { instance_id: 'inst_abc', run_id: 'run_evidence_01', phase: 'cancelling', engaged: true };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const r = await killInstance('inst_abc');

    expect(r.engaged).toBe(true);
    expect(r.phase).toBe('cancelling'); // RunPhase value, verbatim
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(url)).toMatch(/\/agents\/instances\/inst_abc\/kill$/);
    expect((init.method)).toBe('POST');
    expect(new Headers(init.headers).get('authorization')).toBe('Bearer owner-token-xyz');
  });

  it('surfaces a 409 (no active run / run not active) as ApiError(409) — fail-closed, no fabricated success', async () => {
    setAuthTokenProvider(async () => 'owner-token-xyz');
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: { error: 'no_active_run' } }), { status: 409 })));
    await expect(killInstance('inst_dead')).rejects.toMatchObject({ status: 409 });
  });

  it('surfaces a 403 (owned by another) as ApiError(403)', async () => {
    setAuthTokenProvider(async () => 'owner-token-xyz');
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 403 })));
    await expect(killInstance('inst_not_mine')).rejects.toBeInstanceOf(ApiError);
  });
});

describe('armCompetitionKillSwitch — disable execution (engage-only, SAF-004)', () => {
  it('POSTs the EXACT /competitions/{id}/kill-switch path with the bearer and returns the envelope', async () => {
    setAuthTokenProvider(async () => 'owner-token-xyz');
    const body: KillSwitchResult = { competition_id: 'comp_123', kill_switch: true, status: 'kill_switch_on' };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const r = await armCompetitionKillSwitch('comp_123');

    expect(r.kill_switch).toBe(true);
    expect(r.status).toBe('kill_switch_on');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    // EXACT path pin (RED #4): a regression must not drift off the competition kill-switch route.
    expect(String(url)).toMatch(/\/competitions\/comp_123\/kill-switch$/);
    expect((init.method)).toBe('POST');
    expect(new Headers(init.headers).get('authorization')).toBe('Bearer owner-token-xyz');
  });

  it('surfaces a 404 (unknown competition) as ApiError(404)', async () => {
    setAuthTokenProvider(async () => 'owner-token-xyz');
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'not found' }), { status: 404 })));
    await expect(armCompetitionKillSwitch('comp_ghost')).rejects.toMatchObject({ status: 404 });
  });
});
