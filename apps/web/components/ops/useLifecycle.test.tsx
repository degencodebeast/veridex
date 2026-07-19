// F-7: the lifecycle-actions hook — a SEPARATE concern from F-6's runtime-events poll loop. It
// resolves the caller's OWN instance for the open agent (getInstances is owner-scoped), reads the
// authoritative run/lease status, and gates the destructive actions to an OWNED, RUNNING instance.
// Honesty: no owned instance / non-running / demo → not killable → no POST is ever possible.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useLifecycle } from './useLifecycle';

const getInstances = vi.fn();
const getInstanceStatus = vi.fn();
const killInstance = vi.fn();
const armCompetitionKillSwitch = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  getInstances: (...a: unknown[]) => getInstances(...a),
  getInstanceStatus: (...a: unknown[]) => getInstanceStatus(...a),
  killInstance: (...a: unknown[]) => killInstance(...a),
  armCompetitionKillSwitch: (...a: unknown[]) => armCompetitionKillSwitch(...a),
}));

const isMockEnabled = vi.fn(() => false);
vi.mock('@/lib/mock', async (orig) => ({
  ...(await orig<typeof import('@/lib/mock')>()),
  isMockEnabled: () => isMockEnabled(),
}));

const instance = (over: Record<string, unknown> = {}) => ({
  instance_id: 'inst_1', agent_id: 'momentum_fr', run_id: 'run_1', status: 'running',
  ...over,
});
const status = (over: Record<string, unknown> = {}) => ({
  instance_id: 'inst_1', run_id: 'run_1', run_state: 'running', killed: false, status: 'running',
  lease_status: 'active', ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  isMockEnabled.mockReturnValue(false);
  getInstances.mockResolvedValue([instance()]);
  getInstanceStatus.mockResolvedValue(status());
  killInstance.mockResolvedValue({ instance_id: 'inst_1', run_id: 'run_1', phase: 'cancelling', engaged: true });
  armCompetitionKillSwitch.mockResolvedValue({ competition_id: 'comp_1', kill_switch: true, status: 'kill_switch_on' });
});
afterEach(() => { vi.restoreAllMocks(); });

describe('useLifecycle — enablement gate (owned + running only)', () => {
  it('resolves the owned RUNNING instance for the agent → killable', async () => {
    const { result } = renderHook(() => useLifecycle('momentum_fr'));
    await waitFor(() => expect(result.current.killable).toBe(true));
    expect(result.current.instance?.instance_id).toBe('inst_1');
    expect(result.current.status?.run_state).toBe('running');
  });

  it('a NON-running instance (run_state != running) is NOT killable', async () => {
    getInstanceStatus.mockResolvedValue(status({ run_state: 'sealed' }));
    const { result } = renderHook(() => useLifecycle('momentum_fr'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.killable).toBe(false);
  });

  it('an ALREADY-KILLED instance is NOT killable (no double-kill)', async () => {
    getInstanceStatus.mockResolvedValue(status({ run_state: 'cancelled', killed: true }));
    const { result } = renderHook(() => useLifecycle('momentum_fr'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.killable).toBe(false);
  });

  it('NO owned instance for the agent → not killable, and getInstanceStatus is never called', async () => {
    getInstances.mockResolvedValue([]); // owner owns nothing matching this agent
    const { result } = renderHook(() => useLifecycle('momentum_fr'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.killable).toBe(false);
    expect(result.current.instance).toBeNull();
    expect(getInstanceStatus).not.toHaveBeenCalled();
  });

  it('DEMO mode (mock on) → not killable and never touches the network (no faked action)', async () => {
    isMockEnabled.mockReturnValue(true);
    const { result } = renderHook(() => useLifecycle('momentum_fr'));
    await waitFor(() => expect(result.current.demo).toBe(true));
    expect(result.current.killable).toBe(false);
    expect(getInstances).not.toHaveBeenCalled();
    expect(getInstanceStatus).not.toHaveBeenCalled();
  });

  it('a null agentId is inert — no resolution, not killable', async () => {
    const { result } = renderHook(() => useLifecycle(null));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.killable).toBe(false);
    expect(getInstances).not.toHaveBeenCalled();
  });
});

describe('useLifecycle — kill()', () => {
  it('kill() POSTs the kill then refetches status → reflects the TERMINAL run_state', async () => {
    getInstanceStatus
      .mockResolvedValueOnce(status({ run_state: 'running' }))       // initial gate
      .mockResolvedValueOnce(status({ run_state: 'cancelled', killed: true })); // post-kill refetch
    const { result } = renderHook(() => useLifecycle('momentum_fr'));
    await waitFor(() => expect(result.current.killable).toBe(true));

    await act(async () => { await result.current.kill(); });

    expect(killInstance).toHaveBeenCalledWith('inst_1');
    expect(result.current.status?.run_state).toBe('cancelled');
    expect(result.current.killable).toBe(false); // terminal → no re-kill
    expect(result.current.error).toBeNull();
  });

  it('a FAILED kill surfaces a visible error and does NOT claim a terminal/success state', async () => {
    killInstance.mockRejectedValue(new Error('POST /kill failed: 409'));
    const { result } = renderHook(() => useLifecycle('momentum_fr'));
    await waitFor(() => expect(result.current.killable).toBe(true));

    await act(async () => { await result.current.kill(); });

    expect(result.current.error).toMatch(/409|failed/i);
    expect(result.current.status?.run_state).toBe('running'); // unchanged — never a fabricated terminal
  });

  it('kill() is a no-op when not killable (no POST possible)', async () => {
    getInstanceStatus.mockResolvedValue(status({ run_state: 'sealed' }));
    const { result } = renderHook(() => useLifecycle('momentum_fr'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => { await result.current.kill(); });

    expect(killInstance).not.toHaveBeenCalled();
  });
});

describe('useLifecycle — disableExecution() (competition kill-switch)', () => {
  it('canDisableExecution is false without a competitionId and true with one (owned+running)', async () => {
    const { result, rerender } = renderHook(({ c }) => useLifecycle('momentum_fr', c), {
      initialProps: { c: undefined as string | undefined },
    });
    await waitFor(() => expect(result.current.killable).toBe(true));
    expect(result.current.canDisableExecution).toBe(false);
    rerender({ c: 'comp_1' });
    await waitFor(() => expect(result.current.canDisableExecution).toBe(true));
  });

  it('disableExecution() POSTs the competition kill-switch and reflects it', async () => {
    const { result } = renderHook(() => useLifecycle('momentum_fr', 'comp_1'));
    await waitFor(() => expect(result.current.canDisableExecution).toBe(true));

    await act(async () => { await result.current.disableExecution(); });

    expect(armCompetitionKillSwitch).toHaveBeenCalledWith('comp_1');
    expect(result.current.executionDisabled).toBe(true);
    expect(result.current.error).toBeNull();
  });

  it('a FAILED kill-switch surfaces a visible error and does NOT claim execution was disabled', async () => {
    armCompetitionKillSwitch.mockRejectedValue(new Error('POST /kill-switch failed: 404'));
    const { result } = renderHook(() => useLifecycle('momentum_fr', 'comp_1'));
    await waitFor(() => expect(result.current.canDisableExecution).toBe(true));

    await act(async () => { await result.current.disableExecution(); });

    expect(result.current.error).toMatch(/404|failed/i);
    expect(result.current.executionDisabled).toBe(false);
  });
});
