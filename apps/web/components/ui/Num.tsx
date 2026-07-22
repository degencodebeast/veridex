import { signClass, fmtBps } from '@/lib/format';
import styles from './Num.module.css';

export function Num({ value, kind = 'plain' }: { value: number | null | undefined; kind?: 'bps' | 'plain' }) {
  // Absent value (no aggregation / honest "—") renders a neutral em dash — never a fabricated 0.
  if (value == null) return <span data-sign="zero" className={`${styles.num} ${styles.zero} mono`}>—</span>;
  const cls = signClass(value);
  const text = kind === 'bps' ? fmtBps(value) : `${value}`;
  // Scoped CSS-module classes only (no raw global passthrough); data-sign is the stable
  // selector for sign→color (REQ-006/GUD-001). `mono` is a global typography utility.
  return <span data-sign={cls} className={`${styles.num} ${styles[cls]} mono`}>{text}</span>;
}
