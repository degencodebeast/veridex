import { renderJson } from '@/lib/json-syntax';
import styles from './JsonPanel.module.css';

export function JsonPanel({ title, data, accent }: { title?: string; data: unknown; accent?: boolean }) {
  return (
    <div className={`${styles.panel} ${accent ? `accent ${styles.accent}` : ''}`}>
      {title ? <div className={styles.title}>{title}</div> : null}
      <pre className={styles.pre}>{renderJson(data)}</pre>
    </div>
  );
}
