'use client';
import { useEffect, useState } from 'react';
import { CompetitionsScreen } from '@/components/screens/CompetitionsScreen';
import { COMPETITIONS, MY_REWARDS } from '@/lib/fixtures/catalog';
import { isMockEnabled } from '@/lib/mock';
import { getCompetitions, type CompetitionRecordView } from '@/lib/api';
import type { CompetitionSummary, RewardSummary } from '@/lib/catalog';

// /competitions is the "Enter App" landing tab, so honesty here is load-bearing (T-2). Under the
// per-tab mock gate the labeled DEMO fixtures (COMPETITIONS/MY_REWARDS) render unchanged. Off-mock
// the page reads the REAL records (GET /competitions) and passes them via `records` so the screen
// shows a COHERENT count + honest empty state — never the fabricated rich CompetitionSummary cards.
// Gating CLIENT-side is deliberate: isMockEnabled() reads `?mock=1` from window.location.search,
// which a server render cannot see. `records` is passed ONLY off-mock (null → undefined) so the mock
// path never renders the real-records section.
export default function CompetitionsPage() {
  const [comps, setComps] = useState<CompetitionSummary[]>([]);
  const [rewards, setRewards] = useState<RewardSummary[]>([]);
  const [records, setRecords] = useState<CompetitionRecordView[] | null>(null);
  useEffect(() => {
    if (isMockEnabled()) {
      setComps(COMPETITIONS);
      setRewards(MY_REWARDS);
      return;
    }
    let alive = true;
    getCompetitions()
      .then((rows) => { if (alive) setRecords(rows); })
      .catch(() => { if (alive) setRecords([]); }); // honest-empty on error — never fabricated cards
    return () => { alive = false; };
  }, []);
  return <CompetitionsScreen comps={comps} rewards={rewards} records={records ?? undefined} />;
}
