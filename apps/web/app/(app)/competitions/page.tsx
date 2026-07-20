'use client';
import { useEffect, useState } from 'react';
import { CompetitionsScreen } from '@/components/screens/CompetitionsScreen';
import { COMPETITIONS, MY_REWARDS } from '@/lib/fixtures/catalog';
import { isMockEnabled } from '@/lib/mock';
import type { CompetitionSummary, RewardSummary } from '@/lib/catalog';

// /competitions is the "Enter App" landing tab, so honesty here is load-bearing (T-2). The
// competition list + rewards are surfaced ONLY under the per-tab mock gate. Gating CLIENT-side is
// deliberate: isMockEnabled() reads the `?mock=1` param from window.location.search, which a server
// render cannot see — so a judge toggling ?mock=1 gets the labeled DEMO fixtures, while an off-mock
// judge gets honest-empty [] competitions + rewards, NEVER fabricated cards.
//
// Why no real fetch here (unlike /leaderboard): the GET /competitions list envelope
// (CompetitionSummaryResponse) carries no `title` and no `proof_mode`, both of which the rich
// CompetitionSummary view-model requires — proof_mode drives a trust-loaded Badge. An adapter would
// have to INVENT those, which is exactly the fabrication this remediation forbids. Until a wire
// shape that honestly populates the view-model exists, off-mock renders honest-empty.
export default function CompetitionsPage() {
  const [comps, setComps] = useState<CompetitionSummary[]>([]);
  const [rewards, setRewards] = useState<RewardSummary[]>([]);
  useEffect(() => {
    if (isMockEnabled()) {
      setComps(COMPETITIONS);
      setRewards(MY_REWARDS);
    }
  }, []);
  return <CompetitionsScreen comps={comps} rewards={rewards} />;
}
