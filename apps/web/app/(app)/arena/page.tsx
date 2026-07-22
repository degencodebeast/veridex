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

  // The sealed maker benchmark has NO /maker App Router page — its real surface is the contextual
  // Maker Proof Card route (/proof/maker/[id]). Link the rank-1 maker agent (txline-fair-mm); the id
  // is display context only (SEC-005: one sealed maker source, the id never changes which result shows).
  const makerLab = <Link href="/proof/maker/txline-fair-mm">Maker Lab (sealed benchmark) →</Link>;

  if (comps.length === 0) {
    return (
      <>
        <ArenaEmptyState />
        <nav aria-label="Arena divisions">{makerLab}</nav>
      </>
    );
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
      <nav aria-label="Arena divisions">{makerLab}</nav>
    </section>
  );
}
