'use client';
import { useEffect, useState } from 'react';
import { MarketsScreen } from '@/components/screens/MarketsScreen';
import { getFeedHealth, getLeaderboard } from '@/lib/api';
import { isMockEnabled } from '@/lib/mock';
import { ODDS_UPDATES, FIXTURES } from '@/lib/fixtures/catalog';
import type { FeedHealthState, FixtureSummary, LeaderboardRow, OddsUpdate } from '@/lib/catalog';

// T-2 remediation · /markets must NOT show fabricated markets with the demo flag OFF. The four data
// surfaces are sourced honestly and CLIENT-side (isMockEnabled() reads the per-tab `?mock=1` from
// window, which a server render cannot see — so a judge toggling ?mock=1 gets the demo data while an
// off-mock judge gets honest-empty, NEVER the fixture):
//   • odds/fixtures  — NO backend reader/endpoint exists → surface the demo fixture ONLY under the
//                      mock gate; off-mock stays honest-empty ({} / []) so the screen shows absence.
//   • feed-health    — getFeedHealth()  (self-gating: mock → fixture, off-mock → real fetch); null on error.
//   • eligible-agents— getLeaderboard() (self-gating; the eligible rail is leaderboard-derived); [] on error.
export default function MarketsPage() {
  const [oddsByFixture, setOddsByFixture] = useState<Record<number, OddsUpdate[]>>({});
  const [fixtures, setFixtures] = useState<FixtureSummary[]>([]);
  const [feedHealth, setFeedHealth] = useState<FeedHealthState | null>(null);
  const [leaderboard, setLeaderboard] = useState<LeaderboardRow[]>([]);

  useEffect(() => {
    let alive = true;
    // odds/fixtures have no endpoint — the mock flag is the ONLY thing that surfaces the demo fixture.
    if (isMockEnabled()) {
      setOddsByFixture(ODDS_UPDATES);
      setFixtures(FIXTURES);
    }
    getFeedHealth()
      .then((h) => { if (alive) setFeedHealth(h); })
      .catch(() => { if (alive) setFeedHealth(null); }); // honest "unavailable" on error — never a fake feed
    getLeaderboard()
      .then((r) => { if (alive) setLeaderboard(r); })
      .catch(() => { if (alive) setLeaderboard([]); }); // honest-empty on error — never the fixture (T-2)
    return () => { alive = false; };
  }, []);

  return (
    <MarketsScreen
      oddsByFixture={oddsByFixture}
      fixtures={fixtures}
      feedHealth={feedHealth}
      leaderboard={leaderboard}
    />
  );
}
