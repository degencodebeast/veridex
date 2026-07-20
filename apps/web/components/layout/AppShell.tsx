import type { ReactNode } from 'react';
import { TopNav } from './TopNav';
import { SessionWalletChip } from './SessionWalletChip';
import { DirectionRestore } from './DirectionRestore';
import { MockBanner } from './MockBanner';
import { StatusBar } from './StatusBar';
import styles from './AppShell.module.css';

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className={styles.shell}>
      <DirectionRestore />
      <MockBanner />
      <header className={styles.header}>
        <TopNav />
        <div className={styles.right}>
          <SessionWalletChip />
        </div>
      </header>
      <StatusBar />
      <main className={styles.main}>{children}</main>
    </div>
  );
}
