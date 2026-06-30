import type { ReactNode } from 'react';
import { AppShell } from '@/components/layout/AppShell';

// The (app) route group: every product route renders inside the AppShell (top nav + wallet
// chip). The marketing landing at `/` lives outside this group, so it gets standalone chrome.
// Route groups are URL-transparent — paths are unchanged by the (app) segment.
export default function AppGroupLayout({ children }: { children: ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
