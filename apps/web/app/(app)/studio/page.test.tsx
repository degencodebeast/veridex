// F-2: the Agent Studio page must navigate to the REAL instance page (/instances/{instance_id})
// ONLY after the deploy resolves successfully — never the old unconditional /dashboard push, and
// never on a fail-closed preflight. useRouter is mocked to capture the navigation target; fetch is
// stubbed so the deploy never touches the network.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const pushMock = vi.fn();
vi.mock('next/navigation', () => ({ useRouter: () => ({ push: pushMock }) }));

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

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('F-2 · Studio deploy navigation (await-before-navigate)', () => {
  // RED-2: on a SUCCESSFUL deploy the page navigates to the real instance route
  // /instances/{instance_id} — never the old unconditional /dashboard push.
  it('navigates to /instances/{instance_id} on a successful deploy — never /dashboard', async () => {
    const user = userEvent.setup();
    vi.stubGlobal('fetch', vi.fn(async () => okDeploy({
      instance_id: 'inst_x', config_hash: 'c'.repeat(64), policy_hash: 'p'.repeat(64), run_id: 'run_y',
    })));
    render(<AgentStudioPage />);

    await user.click(screen.getByRole('button', { name: /pin config & queue run/i }));

    // The resolved run_id is surfaced (the deploy actually resolved before navigation)…
    expect(await screen.findByTestId('deploy-run-id')).toHaveTextContent('run_y');
    // …and navigation targets the REAL instance page, keyed by the resolved instance_id.
    expect(pushMock).toHaveBeenCalledWith('/instances/inst_x');
    expect(pushMock).not.toHaveBeenCalledWith('/dashboard');
  });

  // RED-2 (companion): a fail-closed preflight (422) STAYS on Studio — the page never navigates.
  it('stays on Studio and never navigates on a fail-closed preflight (422)', async () => {
    const user = userEvent.setup();
    vi.stubGlobal('fetch', vi.fn(async () => failClosedDeploy('feed_health')));
    render(<AgentStudioPage />);

    await user.click(screen.getByRole('button', { name: /pin config & queue run/i }));

    const err = await screen.findByTestId('deploy-preflight-error');
    expect(err).toHaveTextContent(/feed_health/);
    expect(pushMock).not.toHaveBeenCalled();
  });
});
