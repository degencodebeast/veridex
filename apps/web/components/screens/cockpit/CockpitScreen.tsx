'use client';
import { useMemo } from 'react';
import { useArenaStream } from '@/hooks/useArenaStream';
import { usePublishStatus } from '@/components/layout/StatusBarContext';
import type { CockpitState } from '@/lib/contracts';
import type { StatusBarState } from '@/lib/status';
import { RunHeader } from './RunHeader';
import { ProofTraceStrip } from './ProofTraceStrip';
import { MatchStatePanel } from './MatchStatePanel';
import { ClvLeaderboard } from './ClvLeaderboard';
import { CanonicalEventStream } from './CanonicalEventStream';
import { ExecutionLane } from './ExecutionLane';
import { PolicyDecisions } from './PolicyDecisions';
import { QuantityLegend } from '@/components/ui/QuantityLegend';
import styles from './CockpitScreen.module.css';

export function CockpitScreen({ competitionId, initial }: { competitionId: string; initial: CockpitState }) {
  const { state, wsStatus } = useArenaStream(competitionId, initial);

  // Publish the active competition to the shared status bar (the Cockpit is the sole writer);
  // it resets to idle on unmount. source_mode is whatever the header carries (mock ⇒ replay).
  const seq = state.events[0]?.seq ?? state.header.events ?? null;
  const status: StatusBarState = useMemo(() => ({
    fixture: state.header.fixture,
    competition: state.header.competition,
    sourceMode: state.header.source_mode,
    executionMode: state.header.execution_mode,
    ws: wsStatus,
    seq,
    scoring: true,
  }), [state.header.fixture, state.header.competition, state.header.source_mode, state.header.execution_mode, wsStatus, seq]);
  usePublishStatus(status);

  return (
    <div className={styles.cockpit}>
      <RunHeader header={state.header} wsStatus={wsStatus} />
      <ProofTraceStrip trace={state.trace} />
      <div className={styles.grid}>
        <div className={styles.left}>
          <ClvLeaderboard rows={state.leaderboard} />
          <QuantityLegend />
          <CanonicalEventStream runId={state.run_id} events={state.events} />
        </div>
        <div className={styles.right}>
          <MatchStatePanel match={state.match} />
          <ExecutionLane receipts={state.receipts} />
          <PolicyDecisions decisions={state.policy} killArmed={state.kill_armed} />
        </div>
      </div>
    </div>
  );
}
