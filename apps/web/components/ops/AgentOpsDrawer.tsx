'use client';
import { useState, useEffect, useRef } from 'react';
import { Badge } from '@/components/ui/Badge';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { isCanonicalChannel } from '@/lib/catalog';
import { useRuntimeEvents, type AgentOpsState } from './useAgentOps';
import type { CanonicalLogLine, LogChannel, RuntimeOverview, RuntimeStatus } from '@/lib/catalog';
import styles from './AgentOpsDrawer.module.css';

type Tab = 'overview' | 'logs';
type LogFilter = 'all' | 'canonical';

const STATUS_BADGE: Record<RuntimeStatus, 'live' | 'pending' | 'invalid' | 'valid'> = {
  running: 'live', paused: 'pending', failed: 'invalid', completed: 'valid',
};
const dash = (v: number | null) => (v == null ? '—' : String(v));

const LIFECYCLE = ['Pause', 'Resume', 'Kill', 'Rotate creds', 'Disable execution'];

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
      <div className={styles.controls} aria-disabled="true">
        {LIFECYCLE.map((c) => <button key={c} type="button" className={styles.ctl} disabled>{c}</button>)}
      </div>
      <p className={styles.audit}>Control-plane wiring lands in a later phase. When wired, lifecycle actions will be audited and will never edit scored evidence.</p>
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
  state, overviewByAgent, log: logProp,
}: { state: AgentOpsState; overviewByAgent?: Record<string, RuntimeOverview>; log?: CanonicalLogLine[] }) {
  const [tab, setTab] = useState<Tab>('overview');
  const panelRef = useRef<HTMLElement>(null);
  const { isOpen, close } = state;
  // F-6: the LIVE data path. Resolves the owner's instances and cursor-polls durable runtime-events
  // for the open agent (honest-empty/error, never a fixture). Callers MAY inject `overviewByAgent`/
  // `log` to render controlled data (tests, or a future merged competition-event feed); those
  // OVERRIDE the live hook. Fixtures are gone from the default path (T-2).
  const live = useRuntimeEvents(state.agentId);

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
            ? (overview
                ? <OverviewTab o={overview} />
                : <p className={styles.empty}>{live.error ? 'Runtime telemetry unavailable.' : 'No runtime data for this agent.'}</p>)
            : <LogsTab log={log} error={live.error} />}
        </div>
      </aside>
    </div>
  );
}
