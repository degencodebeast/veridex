'use client';
import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { shortHash } from '@/lib/format';
import styles from './WalletChip.module.css';

const MENU_ID = 'wallet-chip-disclosure';

// Presentational only: the real session (connected/address/login/logout) is injected by
// SessionWalletChip, which reads Privy. WalletChip NEVER touches Privy itself — that keeps it
// trivially testable and lets the chrome render in builds where Privy is unconfigured.
type WalletChipProps = {
  connected?: boolean;
  address?: string;
  onConnect?: () => void;
  onDisconnect?: () => void;
  // Privy not yet initialized — render nothing rather than flash a Connect prompt at an operator
  // whose persisted session is about to rehydrate to `connected` (same posture as AuthGate).
  ready?: boolean;
};

export function WalletChip({
  connected = false,
  address,
  onConnect,
  onDisconnect,
  ready = true,
}: WalletChipProps = {}) {
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

  if (!ready) return null;

  // Signed out: a real Connect-Wallet control that fires Privy `login` — NOT a fake operator chip.
  // Disabled (no `onConnect`) only in an unconfigured build where no session is possible anyway.
  if (!connected) {
    return (
      <button type="button" className={styles.connect} onClick={onConnect} disabled={!onConnect}>
        Connect Wallet
      </button>
    );
  }

  // Signed in: the operator chip shows the REAL connected address (truncated), never a placeholder.
  const label = address ? shortHash(address) : 'wallet';

  // Disclosure pattern (not an application menu): a toggle button revealing a plain container of a
  // link + action buttons. No role=menu/menuitem — we do not implement arrow-key roving focus, so
  // advertising it would be a lie.
  return (
    <div className={styles.wrap} ref={ref}>
      <button
        type="button"
        className={styles.chip}
        aria-expanded={open}
        aria-controls={MENU_ID}
        onClick={() => setOpen((v) => !v)}
      >
        <span className={`${styles.op} mono`}>OP {label}</span>
        <span className={styles.sep}>·</span>
        <span className={styles.dash}>Dashboard</span>
      </button>
      {open && (
        <div id={MENU_ID} className={styles.menu}>
          <Link href="/dashboard" className={styles.item}>My Agents</Link>
          <div className={styles.group} aria-label="Account">
            <button type="button" className={styles.item}>Settings</button>
            <button type="button" className={styles.item} onClick={onDisconnect}>Disconnect</button>
          </div>
        </div>
      )}
    </div>
  );
}
