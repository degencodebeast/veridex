import { ProofCheckChip, type CheckStatus } from '@/components/ui/ProofCheckChip';
import { CHECK_ORDER, CHECK_LABELS } from '@/lib/checks';
import type { CheckResult } from '@/lib/contracts';
import styles from './ProofChecksBlock.module.css';

const STATUS_TEXT: Record<CheckStatus, string> = {
  pass: 'PASS', fail: 'FAIL', pending: 'PENDING', not_applicable: 'N/A',
};

export function ProofChecksBlock({ checks }: { checks: CheckResult[] }) {
  const byId = new Map(checks.map((c) => [c.id, c]));
  return (
    <section className={styles.block} aria-label="Proof Checks">
      <div className={styles.head}>
        <span className={styles.title}>PROOF CHECKS</span>
        <span className={styles.subtitle}>trust guarantees — not metrics</span>
      </div>
      <ul className={styles.list}>
        {CHECK_ORDER.map((id) => {
          const check = byId.get(id);
          // Bind to the REAL result; if a check is missing, surface pending (never assume pass).
          const status: CheckStatus = check?.result ?? 'pending';
          return (
            <li key={id} className={styles.row}>
              <ProofCheckChip status={status} />
              <span className={styles.label}>{CHECK_LABELS[id]}</span>
              <span className={`${styles.status} ${styles[status]}`}>{STATUS_TEXT[status]}</span>
              <span className={styles.desc}>{check?.method ?? '—'}</span>
            </li>
          );
        })}
      </ul>
      {/* SEC-001: the Checks block must not mention CLV — performance lives in its own block. */}
      <p className={styles.legend}>Legend: ✓ pass · ! fail/pending · ○ not applicable. Checks certify the run is valid; performance metrics are shown separately.</p>
    </section>
  );
}
