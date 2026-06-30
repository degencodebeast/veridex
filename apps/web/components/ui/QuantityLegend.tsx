import { QUANTITIES, STABLE_PRICE_CAPTION } from '@/lib/doctrine';
import styles from './QuantityLegend.module.css';

export function QuantityLegend() {
  return (
    <section className={styles.legend} aria-label="Strategy quantities">
      <span className={styles.title}>WHAT THE NUMBERS MEAN</span>
      <dl className={styles.grid}>
        {QUANTITIES.map((q) => (
          <div key={q.id} className={styles.item}>
            <dt className={styles.label}>{q.label}</dt>
            <dd className={styles.def}>{q.definition}</dd>
          </div>
        ))}
      </dl>
      <p className={styles.caption}>{STABLE_PRICE_CAPTION}</p>
    </section>
  );
}
