'use client';
import { useDirection } from '@/hooks/useDirection';

// Mounted once in AppShell (every route) so the persisted visual Direction is restored
// app-wide on hard refresh — not only on the two screens that render DirectionToggle (CON-001).
export function DirectionRestore() {
  useDirection();
  return null;
}
