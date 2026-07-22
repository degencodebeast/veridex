'use client';
import { useEffect, useState } from 'react';
import { LeaderboardScreen } from '@/components/screens/LeaderboardScreen';
import { getLeaderboard } from '@/lib/api';
import { isMockEnabled } from '@/lib/mock';
import type { LeaderboardRow } from '@/lib/catalog';

// The DIRECTIONAL leaderboard is sourced through the self-gating getLeaderboard() reader (lib/api.ts):
// mock ON (?mock=1 / NEXT_PUBLIC_VERIDEX_MOCK) → the canonical wire fixture; mock OFF → a real fetch.
// Fetching CLIENT-side is deliberate: isMockEnabled() reads the per-tab `?mock=1` param from
// window.location.search, which a server render cannot see — so a judge toggling ?mock=1 on this tab
// gets the demo data, while an off-mock judge gets honest-empty [] (absence/error), NEVER the fixture.
export default function LeaderboardPage() {
  const [rows, setRows] = useState<LeaderboardRow[]>([]);
  useEffect(() => {
    // Quarantine (spec §6.3): the global /leaderboard is populated ONLY by synthetic /demo/run rows,
    // so no off-mock production surface consumes it. Mock mode is UNCHANGED (shows the wire fixture);
    // off-mock renders the honest empty state (no durable directional source yet).
    if (!isMockEnabled()) { setRows([]); return; }
    let alive = true;
    getLeaderboard()
      .then((r) => { if (alive) setRows(r); })
      .catch(() => { if (alive) setRows([]); }); // honest-empty on error — never the fixture (T-2)
    return () => { alive = false; };
  }, []);
  return <LeaderboardScreen rows={rows} />;
}
