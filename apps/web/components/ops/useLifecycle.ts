'use client';
import { useCallback, useEffect, useState } from 'react';
import {
  getInstances, getInstanceStatus, killInstance, armCompetitionKillSwitch,
  type DeployedInstance, type InstanceStatus,
} from '@/lib/api';
import { isMockEnabled } from '@/lib/mock';

// ---- lifecycle actions (F-7) ------------------------------------------------
//
// A DISTINCT concern from F-6's runtime-events poll loop (useRuntimeEvents): this hook drives the
// Agent Ops drawer's CONTROL-PLANE actions. The drawer opens by AGENT id, but the lifecycle endpoints
// are INSTANCE-scoped (status/kill) and COMPETITION-scoped (kill-switch) — so the honest bridge is
// the same as F-6's: resolve the caller's OWN instances (getInstances is owner-scoped, returns ONLY
// the caller's rows), keep those for the open agent, and act on the one live (running) instance.
//
// This is NOT a poll loop — lifecycle is action-driven: resolve + read status ONCE on open, then
// refetch status only after a kill to reflect the resulting terminal state. Honesty invariants:
//   - the owner gate is "no owned instance resolves ⇒ not killable ⇒ no POST possible" (never a
//     client-side identity check — the backend owns ownership);
//   - only an OWNED, RUNNING, not-yet-killed instance is killable;
//   - DEMO mode (mock on) is never killable and never touches the network (a kill is NEVER faked);
//   - a failed kill / status surfaces a VISIBLE error and NEVER a fabricated terminal/success.

const RUN_STATE_RUNNING = 'running';

export interface LifecycleState {
  demo: boolean;
  loading: boolean;
  instance: DeployedInstance | null;
  status: InstanceStatus | null;
  killable: boolean; // owned + running + not-yet-killed ⇒ destructive Kill allowed
  busy: boolean; // a mutation (kill / disable-execution) is in flight
  error: string | null; // last action/read error — surfaced visibly, never swallowed
  killed: boolean; // an owner kill engaged this session (drives the terminal reflection)
  canDisableExecution: boolean; // a competitionId is known AND the instance is killable
  executionDisabled: boolean; // the competition kill-switch engaged this session
  kill: () => Promise<void>;
  disableExecution: () => Promise<void>;
}

export function useLifecycle(agentId: string | null, competitionId?: string): LifecycleState {
  const demo = isMockEnabled();
  const [loading, setLoading] = useState(false);
  const [instance, setInstance] = useState<DeployedInstance | null>(null);
  const [status, setStatus] = useState<InstanceStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [executionDisabled, setExecutionDisabled] = useState(false);

  // Render-phase reset (React "adjust state on prop change"): when the open agent switches, clear the
  // prior agent's resolved instance/status/error IN THE SAME render so no stale control target leaks.
  const [trackedAgent, setTrackedAgent] = useState(agentId);
  if (agentId !== trackedAgent) {
    setTrackedAgent(agentId);
    setInstance(null);
    setStatus(null);
    setError(null);
    setExecutionDisabled(false);
  }

  // Resolve the owned instance + its authoritative status ONCE when the agent opens (not in demo).
  useEffect(() => {
    if (agentId == null || demo) return undefined;
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const instances = await getInstances();
        if (cancelled) return;
        // Prefer a RUNNING instance for this agent (the live control target); fall back to the first
        // owned match so the drawer still shows an honest non-killable state for a settled instance.
        const mine = instances.filter((i) => i.agent_id === agentId);
        const target = mine.find((i) => i.status === RUN_STATE_RUNNING) ?? mine[0] ?? null;
        setInstance(target);
        if (target) {
          const s = await getInstanceStatus(target.instance_id);
          if (cancelled) return;
          setStatus(s);
        }
        if (!cancelled) { setError(null); setLoading(false); }
      } catch (e) {
        if (!cancelled) { setError(e instanceof Error ? e.message : String(e)); setLoading(false); }
      }
    })();
    return () => { cancelled = true; };
  }, [agentId, demo]);

  const killed = status?.killed === true || status?.run_state === 'cancelled';
  const killable = !demo && !busy && instance != null
    && status?.run_state === RUN_STATE_RUNNING && !killed;
  const canDisableExecution = Boolean(competitionId) && !demo && !busy && instance != null
    && status?.run_state === RUN_STATE_RUNNING;

  const kill = useCallback(async () => {
    if (demo || instance == null || status?.run_state !== RUN_STATE_RUNNING || killed) return;
    setBusy(true);
    setError(null);
    try {
      await killInstance(instance.instance_id);
      // Reflect the AUTHORITATIVE resulting state from the backend — never assume a terminal locally.
      const s = await getInstanceStatus(instance.instance_id);
      setStatus(s);
    } catch (e) {
      // Honest failure: surface the error, leave status UNCHANGED (never a fabricated terminal).
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [demo, instance, status, killed]);

  const disableExecution = useCallback(async () => {
    if (!competitionId || demo || instance == null || status?.run_state !== RUN_STATE_RUNNING) return;
    setBusy(true);
    setError(null);
    try {
      const r = await armCompetitionKillSwitch(competitionId);
      setExecutionDisabled(r.kill_switch === true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [competitionId, demo, instance, status]);

  return {
    demo,
    loading,
    instance,
    status,
    killable,
    busy,
    error,
    killed,
    canDisableExecution,
    executionDisabled,
    kill,
    disableExecution,
  };
}
