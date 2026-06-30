'use client';
import { useState } from 'react';
import { Badge } from '@/components/ui/Badge';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { RUNTIME_OVERVIEW, RUNTIME_LOG } from '@/lib/fixtures/catalog';
import { isCanonicalChannel } from '@/lib/catalog';
import type { AgentOpsState } from './useAgentOps';
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
      <div className={styles.controls}>
        {LIFECYCLE.map((c) => <button key={c} type="button" className={styles.ctl}>{c}</button>)}
      </div>
      <p className={styles.audit}>Lifecycle actions are audited and never edit scored evidence.</p>
    </div>
  );
}

function LogsTab({ log }: { log: CanonicalLogLine[] }) {
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
      <div className={styles.log} data-testid="log">
        {visible.map((l, i) => (
          <div key={i} className={`${styles.line} ${l.channel === 'OPS' ? styles.ops : ''}`}>
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
  state, overviewByAgent = RUNTIME_OVERVIEW, log = RUNTIME_LOG,
}: { state: AgentOpsState; overviewByAgent?: Record<string, RuntimeOverview>; log?: CanonicalLogLine[] }) {
  const [tab, setTab] = useState<Tab>('overview');
  if (!state.isOpen || !state.agentId) return null;
  const overview = overviewByAgent[state.agentId];

  return (
    <div className={styles.scrim} role="dialog" aria-label="Agent Ops" onClick={state.close}>
      <aside className={styles.drawer} onClick={(e) => e.stopPropagation()}>
        <header className={styles.fence}>
          <span className={styles.fenceText}>RUNTIME OBSERVABILITY · READ-ONLY · NOT SCORED</span>
          <button type="button" className={styles.close} onClick={state.close} aria-label="Close">×</button>
        </header>
        <div className={styles.tabs} role="tablist">
          <button type="button" role="tab" aria-selected={tab === 'overview'} className={`${styles.tab} ${tab === 'overview' ? styles.activeTab : ''}`} onClick={() => setTab('overview')}>Overview</button>
          <button type="button" role="tab" aria-selected={tab === 'logs'} className={`${styles.tab} ${tab === 'logs' ? styles.activeTab : ''}`} onClick={() => setTab('logs')}>Logs</button>
        </div>
        <div className={styles.body}>
          {tab === 'overview'
            ? (overview ? <OverviewTab o={overview} /> : <p className={styles.empty}>No runtime data for this agent.</p>)
            : <LogsTab log={log} />}
        </div>
      </aside>
    </div>
  );
}
