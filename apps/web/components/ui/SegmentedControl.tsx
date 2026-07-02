'use client';
import { useRef, type KeyboardEvent } from 'react';
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
  const refs = useRef<(HTMLButtonElement | null)[]>([]);

  // Proper radiogroup keyboard model: single tab stop (roving tabindex on the checked
  // radio), arrow keys move selection + focus, locked options are skipped.
  function handleKeyDown(e: KeyboardEvent<HTMLButtonElement>, currentIndex: number) {
    const NAV = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End'];
    if (!NAV.includes(e.key)) return;
    e.preventDefault();

    const selectable = options
      .map((o, i) => ({ option: o, index: i }))
      .filter((x) => !x.option.locked);
    if (selectable.length === 0) return;

    const pos = selectable.findIndex((x) => x.index === currentIndex);
    const forward = e.key === 'ArrowRight' || e.key === 'ArrowDown';
    let target: { option: SegOption<T>; index: number };
    if (e.key === 'Home') {
      target = selectable[0];
    } else if (e.key === 'End') {
      target = selectable[selectable.length - 1];
    } else if (pos === -1) {
      // Focused option is itself locked/non-selectable: enter from an edge.
      target = forward ? selectable[0] : selectable[selectable.length - 1];
    } else {
      const step = forward ? 1 : -1;
      target = selectable[(pos + step + selectable.length) % selectable.length];
    }

    onChange(target.option.value);
    refs.current[target.index]?.focus();
  }

  return (
    <div className={styles.group} role="radiogroup" aria-label={ariaLabel}>
      {options.map((o, i) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            ref={(el) => { refs.current[i] = el; }}
            type="button"
            role="radio"
            aria-checked={active}
            aria-disabled={o.locked || undefined}
            tabIndex={active ? 0 : -1}
            className={`${styles.seg} ${active ? styles.active : ''} ${o.locked ? styles.locked : ''}`}
            onClick={() => { if (!o.locked) onChange(o.value); }}
            onKeyDown={(e) => handleKeyDown(e, i)}
          >
            {o.label}{o.locked ? ' 🔒' : ''}
          </button>
        );
      })}
    </div>
  );
}
