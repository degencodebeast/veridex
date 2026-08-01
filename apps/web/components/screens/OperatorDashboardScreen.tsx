'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { Num } from '@/components/ui/Num';
import { getInstances, type DeployedInstance } from '@/lib/api';
import type { CompetitionSummary, OpsAlert, PayoutState, RewardSummary, RunSummary } from '@/lib/catalog';
import styles from './OperatorDashboardScreen.module.css';

type AgentsState =
  | { kind: 'loading' }
  | { kind: 'ready'; instances: DeployedInstance[] }
  | { kind: 'error' };

// "Your Agents" is bound to the operator's REAL owned instances (getInstances → owner-scoped,
// bearer-authed). It NEVER falls back to a fixture: an empty/unauthorized/failed load renders an
// honest empty or error state (T-2 fixture prohibition), so a fixture agent can never masquerade
// as a live deployment.
function YourAgents({
  loadInstances,
  onOpenRuntime,
}: {
  loadInstances: () => Promise<DeployedInstance[]>;
  onOpenRuntime: (agentId: string) => void;
}) {
  const [state, setState] = useState<AgentsState>({ kind: 'loading' });

  useEffect(() => {
    let active = true;
    setState({ kind: 'loading' });
    loadInstances()
      .then((instances) => { if (active) setState({ kind: 'ready', instances }); })
      .catch(() => { if (active) setState({ kind: 'error' }); });
    return () => { active = false; };
  }, [loadInstances]);

  return (
    <section className={styles.panel}>
      <h2 className={styles.h2}>Your Agents</h2>
      {state.kind === 'loading' && <p className={styles.hint} data-testid="agents-loading">Loading your agents…</p>}
      {state.kind === 'error' && <p className={styles.hint} data-testid="agents-error">Couldn&apos;t load your agents. Check your session and try again.</p>}
      {state.kind === 'ready' && state.instances.length === 0 && (
        <p className={styles.hint} data-testid="agents-empty">No deployed agents yet. <Link href="/studio" className={styles.inlineLink}>Deploy one in Studio →</Link></p>
      )}
      {state.kind === 'ready' && state.instances.length > 0 && (
        <ul className={styles.list}>
          {state.instances.map((inst) => (
            <li key={inst.instance_id} className={styles.agentRow}>
              <Link href={`/instances/${inst.instance_id}`} className={styles.rowMain}>
                <span className="mono">{inst.instance_id}</span>
                <span className={styles.sub}>{inst.template_id} · {inst.status} · {inst.source_mode}</span>
              </Link>
              <span className={styles.rowMeta}>
                <span className={styles.status} data-status={inst.status}>{inst.status}</span>
                <button type="button" className={styles.runtimeBtn} onClick={() => onOpenRuntime(inst.agent_id)}>
                  ⌬ Runtime
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// `failed` is intentionally excluded: a failed payout is an off-nominal state and is
// rendered as a distinct NEGATIVE span (below), never collapsed into a nominal badge.
const PAYOUT_BADGE: Record<Exclude<PayoutState, 'failed'>, 'pending' | 'partial' | 'valid'> = {
  pending: 'pending', 'design-target': 'pending', 'sponsor-funded': 'partial',
  'manual approval': 'pending', paid: 'valid', '2D implementation': 'pending',
};

export function OperatorDashboardScreen({
  connected = false,
  onOpenRuntime = () => {},
  runs = [],
  comps = [],
  rewards = [],
  alerts = [],
  loadInstances = getInstances,
  onConnect,
}: {
  connected?: boolean;
  onOpenRuntime?: (agentId: string) => void;
  // Runs / Competitions / Rewards / Alerts have NO backend reader (no GET exists), so they default to
  // honest-EMPTY (T-2 anti-Potemkin): off-mock the panels render an honest "nothing yet" state, NEVER
  // a fixture. The owning page injects the DEMO fixtures ONLY under the mock gate (isMockEnabled).
  // Contrast "Your Agents", which is bound to REAL owned instances via getInstances (unchanged).
  runs?: RunSummary[];
  comps?: CompetitionSummary[];
  rewards?: RewardSummary[];
  alerts?: OpsAlert[];
  loadInstances?: () => Promise<DeployedInstance[]>;
  // Fires the real login flow (usePrivy().login), wired from the page. Absent in builds where login
  // is impossible (Privy unconfigured) — then the gate stays informative text, never a dead button.
  onConnect?: () => void;
}) {
  // SEC-008 fail-closed gate: operator-private data (agents/runs/rewards/alerts) is only
  // ever rendered when the operator session is authorized. When disconnected we render an
  // honest prompt and NOTHING private — not hidden-but-present, genuinely absent from the DOM.
  if (!connected) {
    return (
      <section className={styles.screen} aria-label="My Agents">
        <header className={styles.head}>
          <h1 className={styles.title}>My Agents</h1>
        </header>
        <div className={styles.gate} data-testid="connect-gate">
          <p className={styles.gateMsg}>Connect your operator wallet to view your agents, runs, rewards, and alerts.</p>
          {onConnect && (
            <button type="button" className={styles.connectBtn} onClick={onConnect}>Connect wallet</button>
          )}
        </div>
      </section>
    );
  }

  return (
    <section className={styles.screen} aria-label="My Agents">
      <header className={styles.head}>
        <h1 className={styles.title}>My Agents</h1>
        <div className={styles.actions}>
          <Link href="/studio" className={styles.primary}>+ New Agent</Link>
          <Link href="/competitions" className={styles.secondary}>Join Competition</Link>
        </div>
      </header>

      <div className={styles.grid}>
        <div className={styles.col}>
          <YourAgents loadInstances={loadInstances} onOpenRuntime={onOpenRuntime} />

          <section className={styles.panel} data-testid="your-runs">
            <h2 className={styles.h2}>Your Runs</h2>
            {runs.length === 0 ? (
              <p className={styles.hint} data-testid="runs-empty">No runs yet. <Link href="/studio" className={styles.inlineLink}>Deploy an agent to start a run →</Link></p>
            ) : (
              <ul className={styles.list}>
                {runs.map((r) => (
                  <li key={r.run_id} className={styles.runRow}>
                    <Link href={`/proof/${r.run_id}`} className={styles.rowMain}>
                      <span>{r.agent_name}</span>
                      <span className={styles.sub}>{r.run_id} ›</span>
                    </Link>
                    <span className={styles.rowMeta}>
                      <Num value={r.avg_clv_bps} kind="bps" />
                      <Badge variant={r.proof_mode} />
                      <Badge variant={r.anchor_status === 'anchored' ? 'anchored' : 'pending'} />
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>

        <div className={styles.col}>
          <section className={styles.panel} data-testid="your-competitions">
            <h2 className={styles.h2}>Competitions you&apos;re in</h2>
            {comps.length === 0 ? (
              <p className={styles.hint} data-testid="competitions-empty">No competitions yet. <Link href="/competitions" className={styles.inlineLink}>Browse competitions →</Link></p>
            ) : (
              <ul className={styles.list}>
                {comps.map((c) => (
                  <li key={c.competition_id} className={styles.runRow}>
                    <Link href={c.lifecycle === 'live' ? `/arena/${c.competition_id}` : `/competitions`} className={styles.rowMain}>
                      <span>{c.title}</span>
                      <span className={styles.sub}>{c.competition_type} ›</span>
                    </Link>
                    <Badge variant={c.lifecycle === 'live' ? 'live' : c.lifecycle === 'settled' ? 'reproducible' : 'pending'} />
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className={styles.panel} data-testid="your-rewards">
            <h2 className={styles.h2}>Your Rewards</h2>
            {rewards.length === 0 ? (
              <p className={styles.hint} data-testid="rewards-empty">No rewards yet.</p>
            ) : (
              <ul className={styles.list}>
                {rewards.map((r) => (
                  <li key={r.competition_id} className={styles.runRow}>
                    <span className={styles.rowMain}>{r.title}</span>
                    <span className={styles.rowMeta}>
                      <span className={`${styles.amount} mono`}>{r.amount_label}</span>
                      {r.payout_state === 'failed'
                        ? <span className={`${styles.failed} mono`} data-payout="failed">failed</span>
                        : <Badge variant={PAYOUT_BADGE[r.payout_state]}>{r.payout_state}</Badge>}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className={styles.panel} data-testid="alerts-rail">
            <h2 className={styles.h2}>Alerts</h2>
            {alerts.length === 0 ? (
              <p className={styles.hint} data-testid="alerts-empty">No alerts.</p>
            ) : (
              <ul className={styles.list}>
                {alerts.map((a) => (
                  <li key={a.id} className={`${styles.alert} mono`}>
                    <span className={styles[a.kind]}>{a.kind.toUpperCase()}</span> · {a.message}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </div>
    </section>
  );
}
