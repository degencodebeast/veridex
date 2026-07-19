'use client';
import { useState, useEffect, useRef } from 'react';
import { Badge } from '@/components/ui/Badge';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { isCanonicalChannel } from '@/lib/catalog';
import { useRuntimeEvents, type AgentOpsState } from './useAgentOps';
import { useLifecycle, type LifecycleState } from './useLifecycle';
import type { CanonicalLogLine, LogChannel, RuntimeOverview, RuntimeStatus } from '@/lib/catalog';
import styles from './AgentOpsDrawer.module.css';

type Tab = 'overview' | 'logs';
type LogFilter = 'all' | 'canonical';

const STATUS_BADGE: Record<RuntimeStatus, 'live' | 'pending' | 'invalid' | 'valid'> = {
  running: 'live', paused: 'pending', failed: 'invalid', completed: 'valid',
};
const dash = (v: number | null) => (v == null ? '—' : String(v));

function OverviewTab({ o }: { o: RuntimeOverview }) {
  return (
    <div className={styles.overview} data-testid="ops-overview">
      <div className={styles.kv}><span>Status</span><Badge variant={STATUS_BADGE[o.status]}>{o.status}</Badge></div>
      <div className={styles.kv}><span>Run</span><span className="mono">{o.run_id ?? '—'}</span></div>
      <div className={styles.kv}><span>Model latency</span><span className="mono">{dash(o.latest_model_latency_ms)}{o.latest_model_latency_ms != null ? ' ms' : ''}</span></div>
      <div className={styles.kv}><span>Model tokens</span><span className="mono">{dash(o.latest_model_tokens)}</span></div>
      <div className={styles.kv}><span>Last action</span><span className="mono">{o.last_action ?? '—'}</span></div>
      <div className={styles.kv}><span>Schema valid</span><span className="mono">{o.schema_valid == null ? '—' : String(o.schema_valid)}</span></div>
      <div className={styles.kv}><span>Errors / retries / tools</span><span className="mono">{o.errors} / {o.retries} / {o.tool_calls}</span></div>
    </div>
  );
}

// F-7: the OWNER-SCOPED lifecycle control-plane region. A confirm dialog gates the destructive
// action; the POST only fires on confirm and the drawer then reflects the AUTHORITATIVE resulting
// run state (never a locally-assumed terminal). Kill / Disable-execution are enabled ONLY for an
// owned, RUNNING instance — a non-owner / non-running / demo state leaves them disabled (no POST
// possible). Pause/Resume/Rotate-creds have NO backend endpoint (the runtime is shutdown-cancel
// only), so they stay honestly disabled rather than fake an action.
type PendingAction = 'kill' | 'disable-execution' | null;

const CONFIRM_COPY: Record<'kill' | 'disable-execution', { title: string; body: string; cta: string }> = {
  kill: {
    title: 'Kill this run?',
    body: 'This terminates the live run through the owner-gated shutdown-cancel. It is audited and never edits scored evidence. This cannot be undone here.',
    cta: 'Kill run',
  },
  'disable-execution': {
    title: 'Disable execution?',
    body: 'This engages the competition kill-switch and stops all trading. It is engage-only — re-arming is a separate, reconciled operation.',
    cta: 'Disable execution',
  },
};

function ConfirmDialog({
  action, busy, onConfirm, onCancel,
}: { action: 'kill' | 'disable-execution'; busy: boolean; onConfirm: () => void; onCancel: () => void }) {
  const copy = CONFIRM_COPY[action];
  return (
    <div className={styles.confirmScrim} onClick={onCancel}>
      <div
        className={styles.confirmBox}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="lifecycle-confirm-title"
        aria-describedby="lifecycle-confirm-body"
        onClick={(e) => e.stopPropagation()}
      >
        <p id="lifecycle-confirm-title" className={styles.confirmTitle}>{copy.title}</p>
        <p id="lifecycle-confirm-body" className={styles.confirmBody}>{copy.body}</p>
        <div className={styles.confirmActions}>
          <button type="button" className={styles.confirmCancel} onClick={onCancel} disabled={busy}>Cancel</button>
          <button type="button" className={styles.confirmDanger} onClick={onConfirm} disabled={busy}>{copy.cta}</button>
        </div>
      </div>
    </div>
  );
}

function LifecycleControls({ lifecycle }: { lifecycle: LifecycleState }) {
  const [pending, setPending] = useState<PendingAction>(null);
  const { killable, canDisableExecution, busy, error, status, executionDisabled } = lifecycle;

  const confirm = async () => {
    const action = pending;
    setPending(null);
    if (action === 'kill') await lifecycle.kill();
    else if (action === 'disable-execution') await lifecycle.disableExecution();
  };

  return (
    <div className={styles.lifecycle}>
      <div className={styles.controls}>
        <button type="button" className={styles.ctl} disabled={!killable} onClick={() => setPending('kill')}>Kill</button>
        <button type="button" className={styles.ctl} disabled={!canDisableExecution} onClick={() => setPending('disable-execution')}>Disable execution</button>
        <button type="button" className={styles.ctl} disabled title="No pause endpoint — the runtime supports shutdown-cancel only.">Pause</button>
        <button type="button" className={styles.ctl} disabled title="No resume endpoint — the runtime supports shutdown-cancel only.">Resume</button>
        <button type="button" className={styles.ctl} disabled title="Credential rotation is not available from this drawer.">Rotate creds</button>
      </div>
      {status && (
        <p className={styles.runState}>
          Run state <span className="mono" data-testid="lifecycle-run-state">{status.run_state}</span>
        </p>
      )}
      {executionDisabled && <p className={styles.runState}>Execution kill-switch <span className="mono">engaged</span></p>}
      {error && <p role="alert" className={styles.ctlError}>Lifecycle action failed: {error}</p>}
      <p className={styles.audit}>Lifecycle actions are audited and never edit scored evidence.</p>
      <p className={styles.note}>Pause/Resume and credential rotation are not available in this runtime — shutdown-cancel only.</p>
      {pending && <ConfirmDialog action={pending} busy={busy} onConfirm={confirm} onCancel={() => setPending(null)} />}
    </div>
  );
}

function LogsTab({ log, error }: { log: CanonicalLogLine[]; error: string | null }) {
  // SEC-003 / #1: default is CANONICAL — OPS runtime telemetry is hidden until the operator
  // explicitly opts into the All (telemetry) view. The filter reuses the b1 isCanonicalChannel seam.
  const [filter, setFilter] = useState<LogFilter>('canonical');
  const visible = filter === 'canonical' ? log.filter((l) => isCanonicalChannel(l.channel)) : log;
  const derived = (c: LogChannel) => c === 'POLICY' || c === 'EXEC';
  return (
    <div className={styles.logs}>
      <SegmentedControl<LogFilter>
        ariaLabel="Log filter" value={filter} onChange={setFilter}
        options={[{ value: 'canonical', label: 'Canonical only' }, { value: 'all', label: 'All' }]}
      />
      {/* HONESTY (T-2): with no live events the drawer shows an honest empty/unavailable line —
          NEVER a canned fixture. `error` distinguishes a failed owner-scoped read from "nothing yet". */}
      {log.length === 0 && (
        <p className={styles.empty}>{error ? 'Runtime telemetry unavailable.' : 'No runtime events yet.'}</p>
      )}
      <div className={styles.log} data-testid="log">
        {visible.map((l, i) => (
          <div key={`${l.ts}-${l.channel}-${i}`} className={`${styles.line} ${l.channel === 'OPS' ? styles.ops : ''}`}>
            <span className={styles.ts}>{l.ts}</span>
            <span className={`${styles.tag} ${styles[`tag_${l.channel}`]}`}>{l.channel}</span>
            <span className={styles.evt}>{l.event}</span>
            <span className={styles.detail}>{l.detail}</span>
            {l.channel === 'OPS' && <span className={styles.nonScoring}>telemetry · non-scoring</span>}
            {derived(l.channel) && <span className={styles.nonScoring}>derived · non-scoring</span>}
          </div>
        ))}
      </div>
      <p className={styles.note}>OPS is runtime telemetry, never proof. Proof checks live on the Proof Card, not here.</p>
    </div>
  );
}

export function AgentOpsDrawer({
  state, overviewByAgent, log: logProp, competitionId,
}: {
  state: AgentOpsState;
  overviewByAgent?: Record<string, RuntimeOverview>;
  log?: CanonicalLogLine[];
  // F-7: the competition this runtime belongs to, if the caller knows it. Present ⇒ "Disable
  // execution" wires to that competition's kill-switch; absent ⇒ the button stays honestly disabled
  // (the drawer never fabricates a competition id). Existing call sites don't pass it yet.
  competitionId?: string;
}) {
  const [tab, setTab] = useState<Tab>('overview');
  const panelRef = useRef<HTMLElement>(null);
  const { isOpen, close } = state;
  // F-6: the LIVE data path. Resolves the owner's instances and cursor-polls durable runtime-events
  // for the open agent (honest-empty/error, never a fixture). Callers MAY inject `overviewByAgent`/
  // `log` to render controlled data (tests, or a future merged competition-event feed); those
  // OVERRIDE the live hook. Fixtures are gone from the default path (T-2).
  const live = useRuntimeEvents(state.agentId);
  // F-7: the SEPARATE lifecycle control-plane concern (owner-scoped kill/status + disable-execution).
  // Distinct from F-6's poll loop — it resolves the owned instance once and acts on demand.
  const lifecycle = useLifecycle(state.agentId, competitionId);

  // Modal semantics (WalletChip keydown pattern): focus into the dialog on open, Escape closes,
  // and focus is restored to the trigger on close. Listener is cleaned up to avoid leaks.
  useEffect(() => {
    if (!isOpen) return undefined;
    const trigger = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      trigger?.focus?.();
    };
  }, [isOpen, close]);

  if (!state.isOpen || !state.agentId) return null;
  const overview = overviewByAgent ? overviewByAgent[state.agentId] : live.overview;
  const log = logProp ?? live.log;
  const panelId = 'ops-panel';
  const activeTabId = tab === 'overview' ? 'ops-tab-overview' : 'ops-tab-logs';

  return (
    <div className={styles.scrim} onClick={state.close}>
      <aside
        ref={panelRef}
        className={styles.drawer}
        role="dialog"
        aria-modal="true"
        aria-label="Agent Ops"
        aria-labelledby="ops-fence-title"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <header className={styles.fence}>
          <span id="ops-fence-title" className={styles.fenceText}>RUNTIME OBSERVABILITY · READ-ONLY · NOT SCORED</span>
          <button type="button" className={styles.close} onClick={state.close} aria-label="Close">×</button>
        </header>
        <div className={styles.tabs} role="tablist" aria-label="Runtime sections">
          <button type="button" id="ops-tab-overview" role="tab" aria-selected={tab === 'overview'} aria-controls={panelId} className={`${styles.tab} ${tab === 'overview' ? styles.activeTab : ''}`} onClick={() => setTab('overview')}>Overview</button>
          <button type="button" id="ops-tab-logs" role="tab" aria-selected={tab === 'logs'} aria-controls={panelId} className={`${styles.tab} ${tab === 'logs' ? styles.activeTab : ''}`} onClick={() => setTab('logs')}>Logs</button>
        </div>
        <div className={styles.body} id={panelId} role="tabpanel" aria-labelledby={activeTabId}>
          {tab === 'overview'
            ? (
              <>
                {overview
                  ? <OverviewTab o={overview} />
                  : <p className={styles.empty}>{live.error ? 'Runtime telemetry unavailable.' : 'No runtime data for this agent.'}</p>}
                {/* F-7: lifecycle controls render independent of the telemetry-derived overview —
                    they are gated by the authoritative instance status, not by whether events exist. */}
                <LifecycleControls lifecycle={lifecycle} />
              </>
            )
            : <LogsTab log={log} error={live.error} />}
        </div>
      </aside>
    </div>
  );
}
