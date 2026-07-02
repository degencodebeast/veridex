'use client';
import { Badge } from '@/components/ui/Badge';
import { MY_REWARDS } from '@/lib/fixtures/catalog';
import type { PayoutState, RewardSummary } from '@/lib/catalog';
import styles from './PrizeVaultScreen.module.css';

// `failed` is intentionally excluded: a failed payout is rendered as a distinct NEGATIVE span
// (below), never collapsed into a nominal badge (mirrors the Operator Dashboard honesty fix).
const PAYOUT_BADGE: Record<Exclude<PayoutState, 'failed'>, 'pending' | 'partial' | 'valid'> = {
  pending: 'pending', 'design-target': 'pending', 'sponsor-funded': 'partial',
  'manual approval': 'pending', paid: 'valid', '2D implementation': 'pending',
};

export function PrizeVaultScreen({ payouts = MY_REWARDS }: { payouts?: RewardSummary[] }) {
  return (
    <section className={styles.screen} aria-label="Prize Vault">
      <h1 className={styles.title}>Prize Vault</h1>
      <div className={styles.banner}>Designed &amp; visible. Payout wiring lands in Phase 2D — no funds move from this screen.</div>

      <div className={styles.grid}>
        <section className={styles.panel}>
          <h2 className={styles.h2}>Vault</h2>
          <div className={styles.kv}><span>Custody</span><span className="mono">Squads multisig · Solana devnet</span></div>
          <div className={styles.kv}><span>Funding source</span><span className="mono">sponsor-funded</span></div>
          <div className={styles.kv}><span>Reward policy</span><span className="mono">top-CLV eligible agents</span></div>
          <div className={styles.kv}><span>score_root</span><span className="mono">0xscore_8a31f2…</span></div>
          <div className={styles.kv}><span>payout_root</span><span className="mono">0xpayout_pending</span></div>
          <div className={styles.kv}><span>Proposal status</span><Badge variant="pending">proposed · unsigned</Badge></div>
        </section>

        <section className={styles.panel}>
          <h2 className={styles.h2}>Proposed payouts</h2>
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
        </section>
      </div>
    </section>
  );
}
