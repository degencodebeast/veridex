'use client';
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { useReducedMotion } from '@/hooks/useReducedMotion';
import styles from './InfoTip.module.css';

// Accessible ⓘ glossary primitive (shared — Proof Card / Cockpit / Studio / Mobile reuse this).
// A REAL focusable button (not a title-only tooltip, which SRs don't announce reliably). Opens on
// hover + keyboard focus + tap; closes on mouseleave + blur + Escape + outside-tap. Tap OPENS (not
// a toggle) so touch doesn't open-then-immediately-close. The popover is linked via aria-describedby
// (role=tooltip carries no aria-expanded); reveal is instant under prefers-reduced-motion. Copy is
// passed in (single-sourced from lib/glossary).
export function InfoTip({ label, children }: { label: string; children: ReactNode }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const reduced = useReducedMotion();
  const wrapRef = useRef<HTMLSpanElement>(null);

  // Outside pointer dismissal (coherent close for touch, where there is no mouseleave/blur).
  useEffect(() => {
    if (!open) return;
    const onOutside = (e: MouseEvent | TouchEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onOutside);
    document.addEventListener('touchstart', onOutside);
    return () => {
      document.removeEventListener('mousedown', onOutside);
      document.removeEventListener('touchstart', onOutside);
    };
  }, [open]);

  return (
    <span className={styles.wrap} ref={wrapRef}>
      <button
        type="button"
        className={styles.trigger}
        aria-label={`What is ${label}?`}
        aria-describedby={id}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onClick={() => setOpen(true)}
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
