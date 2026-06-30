import styles from './ProofCheckChip.module.css';

export type CheckStatus = 'pass' | 'fail' | 'pending' | 'not_applicable';

const GLYPH: Record<CheckStatus, string> = {
  pass: '✓',
  fail: '!',
  pending: '!',
  not_applicable: '○',
};

const CLASS: Record<CheckStatus, string> = {
  pass: styles.pass,
  fail: styles.fail,
  pending: styles.pending,
  not_applicable: styles.notApplicable,
};

export function ProofCheckChip({ status }: { status: CheckStatus }) {
  return (
    <span className={`${status === 'not_applicable' ? 'notApplicable' : status} ${styles.chip} ${CLASS[status]}`} aria-label={status}>
      {GLYPH[status]}
    </span>
  );
}
