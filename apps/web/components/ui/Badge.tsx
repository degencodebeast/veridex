import type { ReactNode } from 'react';
import { BADGE_META, type BadgeVariant } from '@/lib/badges';
import { LiveDot } from './LiveDot';
import styles from './Badge.module.css';

export function Badge({ variant, children }: { variant: BadgeVariant; children?: ReactNode }) {
  const meta = BADGE_META[variant];
  const variantClass = styles[variantClassName(variant)] ?? '';
  return (
    <span className={`${styles.badge} ${variantClass}`} data-variant={variant}>
      {variant === 'live' ? <LiveDot size={5} /> : meta.glyph ? <span aria-hidden>{meta.glyph}</span> : null}
      <span>{children ?? meta.label}</span>
    </span>
  );
}

// CSS Modules can't key on hyphenated names ergonomically; map to camelCase classes.
function variantClassName(v: BadgeVariant): string {
  return v.replace(/-([a-z])/g, (_, c: string) => c.toUpperCase());
}
