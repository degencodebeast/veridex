'use client';
import { useEffect, useState } from 'react';
import { LeaderboardScreen } from '@/components/screens/LeaderboardScreen';
import { getLeaderboard, getDirectionalLeaderboard } from '@/lib/api';
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
    let alive = true;
    // Mock ON (?mock=1) — UNCHANGED: the canonical wire fixture via the self-gating getLeaderboard().
    if (isMockEnabled()) {
      getLeaderboard()
        .then((r) => { if (alive) setRows(r); })
        .catch(() => { if (alive) setRows([]); }); // honest-empty on error — never the fixture (T-2)
      return () => { alive = false; };
    }
    // Off-mock — the REAL directional board (B3, GET /leaderboard/directional): honest DISPLAY NAMES +
    // replay provenance. A DirectionalRow[] is a valid LeaderboardRow[] for the screen. A fetch error →
    // honest-empty (NEVER a wire fixture, T-2 fixture prohibition).
    getDirectionalLeaderboard()
      .then((r) => { if (alive) setRows(r); })
      .catch(() => { if (alive) setRows([]); });
    return () => { alive = false; };
  }, []);
  return <LeaderboardScreen rows={rows} />;
}
