'use client';
import { usePulse } from '@/hooks/usePulse';
import styles from './LiveDot.module.css';

export function LiveDot({ size = 6, label }: { size?: number; label?: string }) {
  const { pulsing, durationMs } = usePulse();
  return (
    <span
      data-livedot
      aria-hidden={label ? undefined : true}
      role={label ? 'status' : undefined}
      aria-label={label}
      className={`${styles.dot} ${pulsing ? styles.pulse : ''}`}
      style={{ width: size, height: size, animationDuration: `${durationMs}ms` }}
    />
  );
}
