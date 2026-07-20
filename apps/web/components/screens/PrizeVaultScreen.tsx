'use client';
import { Badge } from '@/components/ui/Badge';
import type { PayoutState, RewardSummary } from '@/lib/catalog';
import styles from './PrizeVaultScreen.module.css';

// `failed` is intentionally excluded: a failed payout is rendered as a distinct NEGATIVE span
// (below), never collapsed into a nominal badge (mirrors the Operator Dashboard honesty fix).
const PAYOUT_BADGE: Record<Exclude<PayoutState, 'failed'>, 'pending' | 'partial' | 'valid'> = {
  pending: 'pending', 'design-target': 'pending', 'sponsor-funded': 'partial',
  'manual approval': 'pending', paid: 'valid', '2D implementation': 'pending',
};

// A single demo-only root value, shown ONLY when the owning page confirms the mock gate
// (`demo`). Off-mock these roots do NOT exist yet (no on-chain anchor / no settled payout on
// devnet — payout is design-ahead), so we render an honest not-anchored label instead of a
// fabricated hex that would read as a real proof artifact (T-2 anti-Potemkin).
const DEMO_SCORE_ROOT = '0xscore_8a31f2…';
const DEMO_PAYOUT_ROOT = '0xpayout_pending';

export function PrizeVaultScreen({
  // T-2 remediation · there is NO payouts/rewards backend reader (no GET exists), so `payouts`
  // defaults to honest-EMPTY, NEVER the MY_REWARDS fixture. The owning page injects the labeled
  // DEMO fixtures + `demo` flag ONLY under the mock gate (isMockEnabled). Off-mock a judge sees an
  // honest-empty payout list and honest not-anchored roots — no fabricated proof artifacts.
  payouts = [],
  demo = false,
}: {
  payouts?: RewardSummary[];
  demo?: boolean;
}) {
  return (
    <section className={styles.screen} aria-label="Prize Vault">
      <h1 className={styles.title}>Prize Vault</h1>
      <div className={styles.banner}>Designed &amp; visible. Payout wiring lands in Phase 2D — no funds move from this screen.</div>

      <div className={styles.grid}>
        <section className={styles.panel}>
          <h2 className={styles.h2}>Vault</h2>
          <div className={styles.kv}><span>Custody</span><span className="mono">Squads multisig · Solana devnet (design-ahead)</span></div>
          <div className={styles.kv}><span>Funding source</span><span className="mono">sponsor-funded</span></div>
          <div className={styles.kv}><span>Reward policy</span><span className="mono">top-CLV eligible agents</span></div>
          <div className={styles.kv}>
            <span>score_root</span>
            {demo
              ? <span className={styles.rootDemo}><span className="mono">{DEMO_SCORE_ROOT}</span><span className={styles.demoTag}>demo</span></span>
              : <span className={styles.pending}>not yet anchored</span>}
          </div>
          <div className={styles.kv}>
            <span>payout_root</span>
            {demo
              ? <span className={styles.rootDemo}><span className="mono">{DEMO_PAYOUT_ROOT}</span><span className={styles.demoTag}>demo</span></span>
              : <span className={styles.pending}>no settled payout</span>}
          </div>
          <div className={styles.kv}><span>Proposal status</span><Badge variant="pending">proposed · unsigned</Badge></div>
        </section>

        <section className={styles.panel}>
          <h2 className={styles.h2}>Proposed payouts</h2>
          {payouts.length === 0 ? (
            // Honest-empty: no fabricated payouts when there is no reader (mock OFF). NEVER the
            // MY_REWARDS fixture — payout settlement is design-ahead until 2D custody is wired.
            <p className={styles.empty} data-testid="payout-empty">
              No payouts yet — settlement wiring lands in Phase 2D (no live payout / no Squads custody on devnet).
            </p>
          ) : (
            <>
              <ul className={styles.list} data-testid="payout-list">
                {payouts.map((p) => (
                  <li key={p.competition_id} className={styles.row}>
                    <span className={styles.compTitle}>{p.title}</span>
                    <span className={styles.rowMeta}>
                      <span className={`${styles.amount} mono`}>{p.amount_label}</span>
                      {p.payout_state === 'failed'
                        ? <span className={`${styles.failed} mono`} data-payout="failed">failed</span>
                        : <Badge variant={PAYOUT_BADGE[p.payout_state]}>{p.payout_state}</Badge>}
                    </span>
                  </li>
                ))}
              </ul>
              <p className={styles.note}>Payout states are honest: nothing here is paid until 2D custody/settlement is wired.</p>
            </>
          )}
        </section>
      </div>
    </section>
  );
}
