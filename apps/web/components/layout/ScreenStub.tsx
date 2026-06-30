import styles from './ScreenStub.module.css';

export function ScreenStub({ label, plan, note }: { label: string; plan: string; note?: string }) {
  return (
    <section className={styles.stub} aria-label={`${label} (placeholder)`}>
      <h1 className={styles.title}>{label}</h1>
      <p className={`${styles.badge} mono`}>Built in {plan}</p>
      <p className={styles.note}>
        {note ?? 'This screen is part of the Veridex 2C build sequence and is not implemented in the foundation plan. The route exists so the information architecture is fully navigable.'}
      </p>
    </section>
  );
}
