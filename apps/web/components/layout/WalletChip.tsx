'use client';
import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { CONTEXTUAL_ROUTES } from '@/lib/nav';
import styles from './WalletChip.module.css';

const ACCOUNT_ACTIONS = ['Network: Solana Devnet', 'Settings', 'Disconnect'];

const MENU_ID = 'wallet-chip-disclosure';

export function WalletChip({ address = '9xQe…7vT2' }: { address?: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onClick);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onClick);
    };
  }, [open]);

  // Disclosure pattern (not an application menu): a toggle button revealing a
  // plain container of links + action buttons. No role=menu/menuitem — we do
  // not implement arrow-key roving focus, so advertising it would be a lie.
  return (
    <div className={styles.wrap} ref={ref}>
      <button
        type="button"
        className={styles.chip}
        aria-expanded={open}
        aria-controls={MENU_ID}
        onClick={() => setOpen((v) => !v)}
      >
        <span className={`${styles.op} mono`}>OP {address}</span>
        <span className={styles.sep}>·</span>
        <span className={styles.dash}>Dashboard</span>
      </button>
      {open && (
        <div id={MENU_ID} className={styles.menu}>
          <Link href="/dashboard" className={styles.item}>Operator Dashboard</Link>
          <div className={styles.group} aria-label="Account">
            {ACCOUNT_ACTIONS.map((label) => (
              <button key={label} type="button" className={styles.item}>{label}</button>
            ))}
          </div>
          <div className={styles.groupLabel}>Prototype Screens</div>
          <div className={styles.group} aria-label="Prototype screens">
            {/* Operator Dashboard has its own dedicated link above; exclude it
                here so it is not listed twice (it stays in CONTEXTUAL_ROUTES as
                the IA single source). */}
            {CONTEXTUAL_ROUTES.filter((r) => r.href !== '/dashboard').map((r) => (
              <Link key={r.href} href={r.href} className={styles.item}>{r.label}</Link>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
