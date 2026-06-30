'use client';
import styles from './SegmentedControl.module.css';

export interface SegOption<T extends string> { value: T; label: string; locked?: boolean }

export function SegmentedControl<T extends string>({
  options, value, onChange, ariaLabel,
}: {
  options: SegOption<T>[];
  value: T;
  onChange: (v: T) => void;
  ariaLabel: string;
}) {
  return (
    <div className={styles.group} role="radiogroup" aria-label={ariaLabel}>
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={active}
            aria-disabled={o.locked || undefined}
            className={`${styles.seg} ${active ? styles.active : ''} ${o.locked ? styles.locked : ''}`}
            onClick={() => { if (!o.locked) onChange(o.value); }}
          >
            {o.label}{o.locked ? ' 🔒' : ''}
          </button>
        );
      })}
    </div>
  );
}
