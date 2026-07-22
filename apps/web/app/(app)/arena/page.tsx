'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { ArenaEmptyState } from '@/components/screens/ArenaEmptyState';
import { getCompetitions, type CompetitionRecordView } from '@/lib/api';
import { isMockEnabled } from '@/lib/mock';

// /arena landing (spec §6.2): Directional stays the default division. Instead of a permanent static
// empty state, discover REAL competitions (GET /competitions). The domain lifecycle enum is
// draft | open | running | finalized (veridex/competition/models.py:44-47), and POST
// /competitions/{id}/start SYNCHRONOUSLY persists/returns `finalized` (tests/test_competition_api.py:149-160).
// Discover the two statuses that should surface: `running` (opens the live /arena/{id} cockpit) and
// `finalized` (completed, still discoverable). Draft/open are NOT surfaced. Only REAL enum values are
// used — never invented `started`/`live`/`completed`/`settled`. A minimal Maker Lab entry lives under
// Arena (the sealed benchmark — NOT a directional competition/ranking). Off-mock only; empty → honest state.
const ACTIVE = new Set(['running', 'finalized']);

export default function ArenaPage() {
  const [comps, setComps] = useState<CompetitionRecordView[]>([]);
  useEffect(() => {
    if (isMockEnabled()) return;
    let alive = true;
    getCompetitions()
      .then((rows) => { if (alive) setComps(rows.filter((c) => ACTIVE.has(c.status))); })
      .catch(() => { if (alive) setComps([]); });
    return () => { alive = false; };
  }, []);

  // The sealed maker benchmark's natural home is the Leaderboard "Maker" lane (ranked by
  // adverse-selection toxicity), which live-fetches /maker/arena-result — so Arena does NOT
  // duplicate it as a bare link here.
  if (comps.length === 0) {
    return <ArenaEmptyState />;
  }
  return (
    <section aria-label="Arena — discovered competitions">
      <h1>Arena</h1>
      <ul>
        {comps.map((c) => (
          <li key={c.competitionId}>
            <Link href={`/arena/${c.competitionId}`}>{c.title}</Link>
            <span className="mono"> · {c.status}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
