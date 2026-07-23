'use client';
import { useEffect, useState } from 'react';
import { MarketsScreen } from '@/components/screens/MarketsScreen';
import { getFeedHealth, getLeaderboard, getReplayMarkets, getReplayPacks } from '@/lib/api';
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
    // Quarantine (spec §6.3): the ELIGIBLE AGENTS rail is /leaderboard-derived, and /leaderboard holds
    // ONLY synthetic /demo/run rows off-mock. Fetch it ONLY under mock; off-mock the rail stays empty so
    // smoke-seeded synthetic agents can never surface as production data. Mock mode is UNCHANGED.
    if (isMockEnabled()) {
      getLeaderboard()
        .then((r) => { if (alive) setLeaderboard(r); })
        .catch(() => { if (alive) setLeaderboard([]); });
    }
    // Off-mock the fixtures sidebar is fed by the REAL verified ReplayPack catalog (/replay-packs):
    // each pack's server-side fixture_metadata → a FixtureSummary. Labels are server-sourced, with an
    // honest fallback when label_source==='unavailable' (never a fabricated team name). Under mock the
    // fixtures already come from FIXTURES above and getReplayPacks() returns [] — never overwrite them.
    getReplayPacks()
      .then((packs) => {
        if (!alive || isMockEnabled()) return;
        setFixtures(
          packs.flatMap((pack) =>
            pack.fixtureMetadata.map((m): FixtureSummary => ({
              fixture_id: m.fixture_id,
              pack_id: pack.packId,
              competition: pack.packId,
              participant1: m.home_team ?? `id ${m.fixture_id}`,
              participant2: m.away_team ?? '—',
              // kickoff_ts is epoch SECONDS on the wire → ISO string; absent ⇒ '' (honest, never faked).
              start_time: m.kickoff_ts != null ? new Date(m.kickoff_ts * 1000).toISOString() : '',
              in_running: false, // replay catalog — never in-running
            })),
          ),
        );
        // E2: off-mock the odds table is fed by the REAL replay-market projection. Fetch each catalogued
        // (pack_id, fixture_id)'s LAST-KNOWN odds per market and populate oddsByFixture[fixture_id]. On
        // error each fixture stays honest-empty (getReplayMarkets returns []) — never a fabricated market.
        for (const pack of packs) {
          for (const m of pack.fixtureMetadata) {
            getReplayMarkets(pack.packId, m.fixture_id).then((updates) => {
              if (alive && !isMockEnabled()) setOddsByFixture((prev) => ({ ...prev, [m.fixture_id]: updates }));
            });
          }
        }
      })
      .catch(() => { /* honest-empty: fixtures stay [] off-mock on error — never a fabricated fixture */ });
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
