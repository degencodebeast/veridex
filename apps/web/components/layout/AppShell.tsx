import type { ReactNode } from 'react';
import { TopNav } from './TopNav';
import { WalletChip } from './WalletChip';
import styles from './AppShell.module.css';

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <TopNav />
        <div className={styles.right}>
          <WalletChip />
        </div>
      </header>
      <main className={styles.main}>{children}</main>
    </div>
  );
}
