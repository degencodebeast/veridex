import { ProofCheckChip } from '@/components/ui/ProofCheckChip';
import type { ValidationEntry, ValidationMethod } from '@/lib/contracts';
import styles from './OnChainValidationBlock.module.css';

// The expected data-kind for each method. A mismatch is surfaced, never relabeled (AC-013).
const METHOD_KIND: Record<ValidationMethod, ValidationEntry['data_kind']> = {
  validateOdds: 'odds', validateFixture: 'fixture', validateFixtureBatch: 'fixture', validateStat: 'stat',
};

export function OnChainValidationBlock({ validations }: { validations: ValidationEntry[] }) {
  return (
    <section className={styles.block} aria-label="On-Chain Validation">
      <div className={styles.head}><span className={styles.title}>ON-CHAIN VALIDATION</span></div>
      {validations.length === 0 ? (
        // Gap decision: the per-evidence txoracle validateOdds/validateStat entries are
        // computed at ingest and NOT carried in the proof artifact. Don't fabricate a
        // list — state the honest absence (Phase 2D surfaces it).
        <p className={styles.empty}>
          Per-entry on-chain evidence validation is computed at ingest and not surfaced in this proof artifact (Phase 2D). The Veridex proof anchor is shown in the Anchor panel.
        </p>
      ) : (
        <ul className={styles.list}>
          {validations.map((v, i) => {
            const mismatch = METHOD_KIND[v.method] !== v.data_kind;
            return (
              <li key={`${v.method}-${i}`} className={styles.row}>
                <ProofCheckChip status={v.result} />
                <span className={`${styles.method} mono`}>{v.method}</span>
                <span className={styles.kind}>{v.data_kind}{v.message_id ? ` · ${v.message_id}` : ''}</span>
                <span className={`${styles.root} mono`}>{v.root}</span>
                {mismatch ? <span className={styles.warn}>⚠ label mismatch</span> : <span />}
              </li>
            );
          })}
        </ul>
      )}
      <p className={styles.note}>Each record is verified against TxLINE&apos;s txoracle Merkle root. Odds/scores batched on 5-min intervals; fixture validation is batch-root based.</p>
    </section>
  );
}
