'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { getCockpitState } from '@/lib/api';
import { RunHeader } from './cockpit/RunHeader';
import { ProofTraceStrip } from './cockpit/ProofTraceStrip';
import { MatchStatePanel } from './cockpit/MatchStatePanel';
import { ClvLeaderboard } from './cockpit/ClvLeaderboard';
import { CanonicalEventStream } from './cockpit/CanonicalEventStream';
import type { CockpitState } from '@/lib/contracts';
import styles from './MobileArenaScreen.module.css';

const TABS = [
  { label: 'Arena', href: '/m/arena' }, { label: 'Agents', href: '/agents' },
  { label: 'Proof', href: '/leaderboard' }, { label: 'Rank', href: '/leaderboard' },
];

const COMPETITION_ID = 'wc-fra-bra';

// Honest-empty initial (SSR + before the client projection loads). Live with no backend stays here
// (fetch fails → caught → honest-empty panels); mock populates it via getCockpitState (COCKPIT_DEMO).
const EMPTY_COCKPIT: CockpitState = {
  competition_id: COMPETITION_ID, run_id: '',
  header: {
    fixture: '', competition: '', source_mode: 'replay', execution_mode: 'paper',
    proof_mode: 'reproducible', events: 0, valid_pct: 0, verifier_version: 'v0',
  },
  trace: [],
  match: { fixture: '', phase: 'NS', minute: null, goals: [0, 0], yellow: [0, 0], red: [0, 0], corners: [0, 0], status: 'scheduled' },
  leaderboard: [], events: [], receipts: [], policy: [], kill_armed: false,
};

// Mobile = the Cockpit collapsed to ONE scroll column. It REUSES the closed Cockpit panels + the
// getCockpitState projection (no forked data / honesty logic): MatchState B/C framing, ●evidence/
// ○ui-only, REPLAY≠LIVE, CLV-only rank, and the glossary InfoTips all come along with the panels.
export function MobileArenaScreen({ competitionId = COMPETITION_ID, initial }: {
  competitionId?: string; initial?: CockpitState;
}) {
  const [state, setState] = useState<CockpitState>(initial ?? EMPTY_COCKPIT);

  useEffect(() => {
    if (initial) return; // a supplied projection (tests / SSR seed) skips the client fetch
    let live = true;
    // mock ⇒ populated demo; live with no backend ⇒ caught ⇒ honest-empty (never fabricated).
    getCockpitState(competitionId).then((s) => { if (live) setState(s); }).catch(() => {});
    return () => { live = false; };
  }, [competitionId, initial]);

  return (
    <div className={styles.frame} data-testid="phone-frame" data-width="392">
      {/* one scroll column — the reused Cockpit panels stacked; no live WS on mobile (honest). */}
      <div className={styles.column} data-testid="mobile-column">
        <RunHeader header={state.header} wsStatus="disconnected" />
        <ProofTraceStrip trace={state.trace} />
        <MatchStatePanel match={state.match} />
        {/* the CLV table is wide — allow horizontal scroll rather than fork a mobile leaderboard. */}
        <div className={styles.scrollX}><ClvLeaderboard rows={state.leaderboard} /></div>
        <CanonicalEventStream runId={state.run_id} events={state.events} />
      </div>

      <nav className={styles.tabs} data-testid="bottom-tabs" aria-label="Mobile tabs">
        {TABS.map((t) => (
          <Link key={t.label} href={t.href} className={styles.tab}>{t.label}</Link>
        ))}
      </nav>
    </div>
  );
}
