import { Badge } from '@/components/ui/Badge';
import { shortHash } from '@/lib/format';
import type { AnchorInfo } from '@/lib/contracts';
import styles from './AnchorPanel.module.css';

const STATUS_VARIANT = { anchored: 'anchored', pending: 'pending', not_applicable: 'not-anchored', 'not-anchored': 'not-anchored' } as const;

export function AnchorPanel({ anchor }: { anchor: AnchorInfo }) {
  return (
    <section className={styles.panel} aria-label="Anchor">
      <div className={styles.head}>
        <span className={styles.title}>ANCHOR</span>
        <Badge variant={STATUS_VARIANT[anchor.status]} />
      </div>
      <dl className={styles.grid}>
        <dt className={styles.label}>tx signature</dt>
        <dd className={`${styles.value} mono`}>{anchor.tx_signature ? shortHash(anchor.tx_signature) : '—'}</dd>
        <dt className={styles.label}>manifest hash</dt>
        <dd className={`${styles.value} mono`}>{anchor.manifest_hash ? shortHash(anchor.manifest_hash) : 'verify to reveal'}</dd>
        <dt className={styles.label}>cluster</dt>
        <dd className={`${styles.value} mono`}>{anchor.cluster}</dd>
        <dt className={styles.label}>slot</dt>
        <dd className={`${styles.value} mono`}>{anchor.slot ?? '—'}</dd>
        <dt className={styles.label}>committed</dt>
        <dd className={`${styles.value} mono`}>{anchor.committed_at ? new Date(anchor.committed_at * 1000).toISOString() : '—'}</dd>
      </dl>
      <p className={styles.note}>{anchor.batching_note}</p>
      {anchor.explorer_url ? (
        <a className={styles.explorer} href={anchor.explorer_url} target="_blank" rel="noreferrer">View on Explorer →</a>
      ) : (
        <span className={styles.pendingNote}>Explorer link appears once the on-chain batch is committed.</span>
      )}
    </section>
  );
}
