import { numericClass } from '@/lib/derive';
import { fmtBps } from '@/lib/format';
import styles from './Num.module.css';

export function Num({ value, kind = 'plain' }: { value: number; kind?: 'bps' | 'plain' }) {
  const cls = numericClass(value);
  const text = kind === 'bps' ? fmtBps(value) : `${value}`;
  return <span className={`${cls} ${styles.num} ${styles[cls]} mono num`}>{text}</span>;
}
