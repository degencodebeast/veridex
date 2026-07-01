'use client';
import { useId, useState, type ReactNode } from 'react';
import { useReducedMotion } from '@/hooks/useReducedMotion';
import styles from './InfoTip.module.css';

// Accessible ⓘ glossary primitive (shared — Proof Card / Cockpit / Studio / Mobile reuse this).
// A REAL focusable button (not a title-only tooltip, which SRs don't announce reliably): opens on
// hover, keyboard focus, AND tap; the popover is linked via aria-describedby; Escape closes; the
// reveal is instant under prefers-reduced-motion. Copy is passed in (single-sourced from lib/glossary).
export function InfoTip({ label, children }: { label: string; children: ReactNode }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const reduced = useReducedMotion();
  return (
    <span className={styles.wrap}>
      <button
        type="button"
        className={styles.trigger}
        aria-label={`What is ${label}?`}
        aria-describedby={id}
        aria-expanded={open}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => { if (e.key === 'Escape') setOpen(false); }}
      >
        <span aria-hidden>ⓘ</span>
      </button>
      {/* Kept in the DOM (opacity-toggled, not display:none) so aria-describedby always resolves. */}
      <span id={id} role="tooltip" data-open={open} data-reveal={reduced ? 'instant' : 'anim'} className={styles.popover}>
        {children}
      </span>
    </span>
  );
}
