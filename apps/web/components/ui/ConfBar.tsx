import { clvConfidence, isLowSample, type ClvConfidence } from '@/lib/derive';
import styles from './ConfBar.module.css';

const LABEL: Record<ClvConfidence, string> = { high: 'HIGH', medium: 'MED', low: 'LOW' };
const FILLED: Record<ClvConfidence, number> = { high: 3, medium: 2, low: 1 };

export function ConfBar({ validCount }: { validCount: number }) {
  const conf = clvConfidence(validCount);
  const low = isLowSample(validCount);
  return (
    <span className={styles.wrap}>
      <span className={`${styles.label} mono`}>CONF · {LABEL[conf]}</span>
      <span className={styles.bars} aria-hidden>
        {[0, 1, 2].map((i) => (
          <span key={i} className={`${styles.bar} ${i < FILLED[conf] ? styles[conf] : ''}`} />
        ))}
      </span>
      <span className={`${styles.n} mono`}>n={validCount}</span>
      {low && <span className={`${styles.lowFlag} mono`}>low sample</span>}
    </span>
  );
}
