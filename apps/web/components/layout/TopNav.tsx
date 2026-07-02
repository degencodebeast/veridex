'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { NAV_SECTIONS, isActiveSection } from '@/lib/nav';
import styles from './TopNav.module.css';

export function TopNav() {
  const pathname = usePathname();
  return (
    <nav className={styles.nav} aria-label="Primary">
      <Link href="/" className={styles.logo} aria-label="Veridex home">V</Link>
      <ul className={styles.sections}>
        {NAV_SECTIONS.map((s) => {
          const active = isActiveSection(pathname, s.href);
          return (
            <li key={s.href}>
              <Link
                href={s.href}
                className={`${styles.link} ${active ? styles.active : ''}`}
                aria-current={active ? 'page' : undefined}
              >
                {s.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
