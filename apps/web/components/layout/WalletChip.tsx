'use client';
import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { CONTEXTUAL_ROUTES } from '@/lib/nav';
import styles from './WalletChip.module.css';

const ACCOUNT_ACTIONS = [
  { label: 'Network: Solana Devnet', href: '#network' },
  { label: 'Settings', href: '#settings' },
  { label: 'Disconnect', href: '#disconnect' },
];

export function WalletChip({ address = '0x7Af3…21bC' }: { address?: string }) {
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

  return (
    <div className={styles.wrap} ref={ref}>
      <button
        type="button"
        className={styles.chip}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className={`${styles.op} mono`}>OP {address}</span>
        <span className={styles.sep}>·</span>
        <span className={styles.dash}>Dashboard</span>
      </button>
      {open && (
        <div className={styles.menu} role="menu">
          <Link href="/dashboard" role="menuitem" className={styles.item}>Operator Dashboard</Link>
          <div className={styles.group} role="group" aria-label="Account">
            {ACCOUNT_ACTIONS.map((a) => (
              <a key={a.label} href={a.href} role="menuitem" className={styles.item}>{a.label}</a>
            ))}
          </div>
          <div className={styles.groupLabel}>Prototype Screens</div>
          <div className={styles.group} role="group" aria-label="Prototype screens">
            {/* Operator Dashboard has its own dedicated link above; exclude it
                here so it is not listed twice (it stays in CONTEXTUAL_ROUTES as
                the IA single source). */}
            {CONTEXTUAL_ROUTES.filter((r) => r.href !== '/dashboard').map((r) => (
              <Link key={r.href} href={r.href} role="menuitem" className={styles.item}>{r.label}</Link>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
