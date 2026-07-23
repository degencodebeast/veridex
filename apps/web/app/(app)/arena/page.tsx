'use client';
import { useEffect, useState } from 'react';
import { ArenaEmptyState } from '@/components/screens/ArenaEmptyState';
import { CockpitScreen } from '@/components/screens/cockpit/CockpitScreen';
import { getCompetitions, getCockpitState, getFeedHealth, type CompetitionRecordView } from '@/lib/api';
import { isMockEnabled } from '@/lib/mock';
import type { CockpitState, FeedHealthState } from '@/lib/contracts';
import styles from './arena.module.css';

// /arena landing (spec §6.2): Directional stays the default division. Instead of a permanent static
// empty state, discover REAL competitions (GET /competitions). The domain lifecycle enum is
// draft | open | running | finalized (veridex/competition/models.py:44-47), and POST
// /competitions/{id}/start SYNCHRONOUSLY persists/returns `finalized` (tests/test_competition_api.py:149-160).
// Discover the two statuses that should surface: `running` (a live cockpit stream) and `finalized`
// (completed, still discoverable). Draft/open are NOT surfaced. Only REAL enum values are used.
//
// The landing is itself fixture-selectable: a FIXTURE <select> over the discovered competitions,
// defaulting to the first (most recent), renders THAT competition's existing cockpit INLINE
// (CockpitScreen — the same rich view /arena/[id] serves), swapping when the selection changes.
// Off-mock only; empty/error → the honest ArenaEmptyState, never a fabricated cockpit.
const ACTIVE = new Set(['running', 'finalized']);

interface LoadedCockpit {
  id: string;
  state: CockpitState;
  feedHealth?: FeedHealthState;
}

export default function ArenaPage() {
  // null = still discovering (render nothing, not a fabricated skeleton); [] = discovered-empty.
  const [comps, setComps] = useState<CompetitionRecordView[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [cockpit, setCockpit] = useState<LoadedCockpit | null>(null);

  // Discover the ACTIVE competitions (off-mock only; honest-empty on error — never a fabricated arena).
  useEffect(() => {
    if (isMockEnabled()) { setComps([]); return; }
    let alive = true;
    getCompetitions()
      .then((rows) => {
        if (!alive) return;
        const active = rows.filter((c) => ACTIVE.has(c.status));
        setComps(active);
        setSelected(active[0]?.competitionId ?? null); // default to the first discovered (most recent)
      })
      .catch(() => { if (alive) { setComps([]); setSelected(null); } });
    return () => { alive = false; };
  }, []);

  // Load the selected competition's cockpit snapshot (the CockpitScreen `initial`); the WS then takes
  // over live. Honest-empty on error — the cockpit is simply not rendered, never fabricated.
  useEffect(() => {
    if (!selected) { setCockpit(null); return; }
    let alive = true;
    const id = selected;
    Promise.all([getCockpitState(id), getFeedHealth().catch(() => undefined)])
      .then(([state, feedHealth]) => { if (alive) setCockpit({ id, state, feedHealth: feedHealth ?? undefined }); })
      .catch(() => { if (alive) setCockpit(null); });
    return () => { alive = false; };
  }, [selected]);

  if (comps === null) return null;                 // discovering — no fabricated placeholder
  if (comps.length === 0) return <ArenaEmptyState />;

  return (
    <section aria-label="Arena" className={styles.arena}>
      <div className={styles.fixtureRow}>
        <div className={styles.sel}>
          <label htmlFor="arena-fixture" className={styles.label}>FIXTURE</label>
          <select
            id="arena-fixture"
            className={styles.select}
            value={selected ?? ''}
            onChange={(e) => setSelected(e.target.value)}
          >
            {comps.map((c) => (
              <option key={c.competitionId} value={c.competitionId}>
                {c.title} · {c.competitionId.slice(0, 8)} · {c.status}
              </option>
            ))}
          </select>
        </div>
      </div>
      {cockpit && cockpit.id === selected ? (
        <CockpitScreen competitionId={cockpit.id} initial={cockpit.state} initialFeedHealth={cockpit.feedHealth} />
      ) : null}
    </section>
  );
}
