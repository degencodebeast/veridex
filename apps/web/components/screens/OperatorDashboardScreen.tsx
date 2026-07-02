'use client';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { Num } from '@/components/ui/Num';
import { MY_AGENTS, MY_RUNS, MY_REWARDS, ALERTS, COMPETITIONS } from '@/lib/fixtures/catalog';
import type { PayoutState, RewardSummary } from '@/lib/catalog';
import styles from './OperatorDashboardScreen.module.css';

// `failed` is intentionally excluded: a failed payout is an off-nominal state and is
// rendered as a distinct NEGATIVE span (below), never collapsed into a nominal badge.
const PAYOUT_BADGE: Record<Exclude<PayoutState, 'failed'>, 'pending' | 'partial' | 'valid'> = {
  pending: 'pending', 'design-target': 'pending', 'sponsor-funded': 'partial',
  'manual approval': 'pending', paid: 'valid', '2D implementation': 'pending',
};

export function OperatorDashboardScreen({
  connected = false,
  onOpenRuntime = () => {},
  rewards = MY_REWARDS,
}: { connected?: boolean; onOpenRuntime?: (agentId: string) => void; rewards?: RewardSummary[] }) {
  // SEC-008 fail-closed gate: operator-private data (agents/runs/rewards/alerts) is only
  // ever rendered when the operator session is authorized. When disconnected we render an
  // honest prompt and NOTHING private — not hidden-but-present, genuinely absent from the DOM.
  if (!connected) {
    return (
      <section className={styles.screen} aria-label="Operator Dashboard">
        <header className={styles.head}>
          <h1 className={styles.title}>Operator Dashboard</h1>
        </header>
        <div className={styles.gate} data-testid="connect-gate">
          <p className={styles.gateMsg}>Connect your operator wallet to view your agents, runs, rewards, and alerts.</p>
        </div>
      </section>
    );
  }

  return (
    <section className={styles.screen} aria-label="Operator Dashboard">
      <header className={styles.head}>
        <h1 className={styles.title}>Operator Dashboard</h1>
        <div className={styles.actions}>
          <Link href="/studio" className={styles.primary}>+ New Agent</Link>
          <Link href="/competitions" className={styles.secondary}>Join Competition</Link>
        </div>
      </header>

      <div className={styles.grid}>
        <div className={styles.col}>
          <section className={styles.panel}>
            <h2 className={styles.h2}>Your Agents</h2>
            <ul className={styles.list}>
              {MY_AGENTS.map((a) => (
                <li key={a.agent_id} className={styles.agentRow}>
                  <Link href={`/agents/${a.agent_id}`} className={styles.rowMain}>
                    <span>{a.agent_name}</span>
                    <span className={styles.sub}>{a.archetype} · {a.mode} · {a.source}</span>
                  </Link>
                  <span className={styles.rowMeta}>
                    <Badge variant={a.proof_mode} />
                    <button type="button" className={styles.runtimeBtn} onClick={() => onOpenRuntime(a.agent_id)}>
                      ⌬ Runtime
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          </section>

          <section className={styles.panel}>
            <h2 className={styles.h2}>Your Runs</h2>
            <ul className={styles.list}>
              {MY_RUNS.map((r) => (
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
          </section>
        </div>

        <div className={styles.col}>
          <section className={styles.panel}>
            <h2 className={styles.h2}>Competitions you&apos;re in</h2>
            <ul className={styles.list}>
              {COMPETITIONS.map((c) => (
                <li key={c.competition_id} className={styles.runRow}>
                  <Link href={c.lifecycle === 'live' ? `/arena/${c.competition_id}` : `/competitions`} className={styles.rowMain}>
                    <span>{c.title}</span>
                    <span className={styles.sub}>{c.competition_type} ›</span>
                  </Link>
                  <Badge variant={c.lifecycle === 'live' ? 'live' : c.lifecycle === 'settled' ? 'reproducible' : 'pending'} />
                </li>
              ))}
            </ul>
          </section>

          <section className={styles.panel} data-testid="your-rewards">
            <h2 className={styles.h2}>Your Rewards</h2>
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
          </section>

          <section className={styles.panel} data-testid="alerts-rail">
            <h2 className={styles.h2}>Alerts</h2>
            <ul className={styles.list}>
              {ALERTS.map((a) => (
                <li key={a.id} className={`${styles.alert} mono`}>
                  <span className={styles[a.kind]}>{a.kind.toUpperCase()}</span> · {a.message}
                </li>
              ))}
            </ul>
          </section>
        </div>
      </div>
    </section>
  );
}
