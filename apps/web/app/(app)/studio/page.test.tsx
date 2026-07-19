// F-2: the Agent Studio page must (1) navigate to the real instance page only AFTER a successful
// deploy, and (2) gate the DEPLOY AFFORDANCE behind auth — an unauthenticated (or an unconfigured-
// Privy) operator must NOT see an actionable deploy button and must NOT be able to fire a bearer-less
// owner-scoped POST. useRouter + usePrivy are mocked; fetch is stubbed so no test touches the network.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const pushMock = vi.fn();
vi.mock('next/navigation', () => ({ useRouter: () => ({ push: pushMock }) }));

const usePrivyMock = vi.fn();
vi.mock('@privy-io/react-auth', () => ({ usePrivy: () => usePrivyMock() }));

import AgentStudioPage from './page';

// A resolved deploy response (pinned instance + async run_id).
function okDeploy(body?: Record<string, unknown>) {
  return {
    ok: true,
    status: 200,
    json: async () => body ?? {
      instance_id: 'inst_demo', config_hash: 'a'.repeat(64), policy_hash: 'b'.repeat(64), run_id: 'run_demo',
    },
  } as unknown as Response;
}

// A fail-closed preflight 422 body naming the failing check(s).
function failClosedDeploy(...failed: string[]) {
  return {
    ok: false,
    status: 422,
    json: async () => ({
      detail: {
        error: 'preflight_failed',
        failed_checks: failed,
        checks: failed.map((name) => ({ name, ok: false, detail: `${name} not ready` })),
      },
    }),
  } as unknown as Response;
}

// An authenticated, ready Privy session (deploy permitted).
function signIn() {
  usePrivyMock.mockReturnValue({ ready: true, authenticated: true, login: vi.fn(), getAccessToken: vi.fn() });
}

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('F-2 · Studio deploy navigation (await-before-navigate, authenticated path)', () => {
  // RED-2: on a SUCCESSFUL deploy the page navigates to the real instance route
  // /instances/{instance_id} — never the old unconditional /dashboard push.
  it('navigates to /instances/{instance_id} on a successful deploy — never /dashboard', async () => {
    vi.stubEnv('NEXT_PUBLIC_PRIVY_APP_ID', 'app-123');
    signIn();
    const user = userEvent.setup();
    vi.stubGlobal('fetch', vi.fn(async () => okDeploy({
      instance_id: 'inst_x', config_hash: 'c'.repeat(64), policy_hash: 'p'.repeat(64), run_id: 'run_y',
    })));
    render(<AgentStudioPage />);

    await user.click(screen.getByRole('button', { name: /pin config & queue run/i }));

    expect(await screen.findByTestId('deploy-run-id')).toHaveTextContent('run_y');
    expect(pushMock).toHaveBeenCalledWith('/instances/inst_x');
    expect(pushMock).not.toHaveBeenCalledWith('/dashboard');
  });

  // RED-2 (companion): a fail-closed preflight (422) STAYS on Studio — the page never navigates.
  it('stays on Studio and never navigates on a fail-closed preflight (422)', async () => {
    vi.stubEnv('NEXT_PUBLIC_PRIVY_APP_ID', 'app-123');
    signIn();
    const user = userEvent.setup();
    vi.stubGlobal('fetch', vi.fn(async () => failClosedDeploy('feed_health')));
    render(<AgentStudioPage />);

    await user.click(screen.getByRole('button', { name: /pin config & queue run/i }));

    const err = await screen.findByTestId('deploy-preflight-error');
    expect(err).toHaveTextContent(/feed_health/);
    expect(pushMock).not.toHaveBeenCalled();
  });
});

describe('F-2 · Studio deploy affordance is auth-gated (Codex Item 1, program.json:2711)', () => {
  // (a) signed-out (Privy configured, not authenticated): the login affordance replaces the deploy
  // button — the button is ABSENT from the DOM and no owner-scoped POST can fire.
  it('signed-out: shows the login gate, the deploy button is ABSENT, and no fetch fires', () => {
    vi.stubEnv('NEXT_PUBLIC_PRIVY_APP_ID', 'app-123');
    const login = vi.fn();
    usePrivyMock.mockReturnValue({ ready: true, authenticated: false, login, getAccessToken: vi.fn() });
    const fetchMock = vi.fn(async () => okDeploy());
    vi.stubGlobal('fetch', fetchMock);
    render(<AgentStudioPage />);

    // The deploy button is structurally absent; the login gate stands in its place.
    expect(screen.queryByRole('button', { name: /pin config & queue run/i })).toBeNull();
    const gate = screen.getByTestId('auth-login-gate');
    expect(gate).toBeInTheDocument();
    // The signed-out operator may still SEE the config draft (they just can't deploy).
    expect(screen.getByLabelText(/archetype/i)).toBeInTheDocument();
    // No bearer-less deploy can fire; the gate's control is wired to the real Privy login.
    expect(fetchMock).not.toHaveBeenCalled();
    within(gate).getByRole('button', { name: /log in/i }).click();
    expect(login).toHaveBeenCalledTimes(1);
  });

  // (b) unconfigured env (no NEXT_PUBLIC_PRIVY_APP_ID): the deploy area shows an EXPLICIT fail-closed
  // prompt (not a blank, not an actionable deploy), and the page never reads usePrivy (which throws
  // outside <PrivyProvider>).
  it('unconfigured env: shows an explicit fail-closed deploy prompt, no deploy button, never reads usePrivy', () => {
    const fetchMock = vi.fn(async () => okDeploy());
    vi.stubGlobal('fetch', fetchMock);
    render(<AgentStudioPage />);

    expect(usePrivyMock).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /pin config & queue run/i })).toBeNull();
    expect(screen.getByTestId('deploy-auth-required')).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  // (c) authenticated: the deploy button renders and works end-to-end (fires the deploy, navigates).
  it('authenticated: the deploy button renders and deploys (navigates on success)', async () => {
    vi.stubEnv('NEXT_PUBLIC_PRIVY_APP_ID', 'app-123');
    signIn();
    const user = userEvent.setup();
    vi.stubGlobal('fetch', vi.fn(async () => okDeploy({
      instance_id: 'inst_x', config_hash: 'c'.repeat(64), policy_hash: 'p'.repeat(64), run_id: 'run_y',
    })));
    render(<AgentStudioPage />);

    await user.click(screen.getByRole('button', { name: /pin config & queue run/i }));

    expect(await screen.findByTestId('deploy-run-id')).toHaveTextContent('run_y');
    expect(pushMock).toHaveBeenCalledWith('/instances/inst_x');
  });
});
