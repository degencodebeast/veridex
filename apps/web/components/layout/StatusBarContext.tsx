'use client';
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { mockStatusSeed } from '@/lib/api';
import type { StatusBarState } from '@/lib/status';

interface StatusBarCtx {
  status: StatusBarState | null;
  setStatus: (s: StatusBarState | null) => void;
}

const Ctx = createContext<StatusBarCtx>({ status: null, setStatus: () => {} });

export function useStatusBar(): StatusBarCtx {
  return useContext(Ctx);
}

// Publishes the active competition's status to the bar while a screen is mounted, and resets to
// idle on unmount (leaving the arena clears the bar honestly). The Arena/Cockpit is the sole writer.
export function usePublishStatus(state: StatusBarState | null): void {
  const { setStatus } = useStatusBar();
  useEffect(() => {
    setStatus(state);
    return () => setStatus(null);
  }, [state, setStatus]);
}

export function StatusBarProvider({ children }: { children: ReactNode }) {
  const [published, setPublished] = useState<StatusBarState | null>(null);
  // Mock seed (effect, not initializer) so the `?mock=1` path applies without an SSR mismatch.
  const [seed, setSeed] = useState<StatusBarState | null>(null);
  useEffect(() => { setSeed(mockStatusSeed()); }, []);
  // A live publish (arena) wins; otherwise the mock seed (or idle when neither).
  return <Ctx.Provider value={{ status: published ?? seed, setStatus: setPublished }}>{children}</Ctx.Provider>;
}
