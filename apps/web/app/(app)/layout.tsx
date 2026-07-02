import type { ReactNode } from 'react';
import { AppShell } from '@/components/layout/AppShell';
import { StatusBarProvider } from '@/components/layout/StatusBarContext';

// The (app) route group: every product route renders inside the AppShell (top nav + wallet
// chip + shared status bar). The marketing landing at `/` lives outside this group, so it gets
// standalone chrome. Route groups are URL-transparent — paths are unchanged by the (app) segment.
// StatusBarProvider wraps the shell so the bar (in AppShell) and the Arena screen (in children)
// share the active-competition context.
export default function AppGroupLayout({ children }: { children: ReactNode }) {
  return (
    <StatusBarProvider>
      <AppShell>{children}</AppShell>
    </StatusBarProvider>
  );
}
