import { signClass, fmtBps } from '@/lib/format';
import styles from './Num.module.css';

export function Num({ value, kind = 'plain' }: { value: number; kind?: 'bps' | 'plain' }) {
  const cls = signClass(value);
  const text = kind === 'bps' ? fmtBps(value) : `${value}`;
  // Scoped CSS-module classes only (no raw global passthrough); data-sign is the stable
  // selector for sign→color (REQ-006/GUD-001). `mono` is a global typography utility.
  return <span data-sign={cls} className={`${styles.num} ${styles[cls]} mono`}>{text}</span>;
}
