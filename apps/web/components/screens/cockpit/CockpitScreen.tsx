'use client';
import { useArenaStream } from '@/hooks/useArenaStream';
import type { CockpitState } from '@/lib/contracts';
import { RunHeader } from './RunHeader';
import { ProofTraceStrip } from './ProofTraceStrip';
import { MatchStatePanel } from './MatchStatePanel';
import { ClvLeaderboard } from './ClvLeaderboard';
import { CanonicalEventStream } from './CanonicalEventStream';
import { ExecutionLane } from './ExecutionLane';
import { PolicyDecisions } from './PolicyDecisions';
import styles from './CockpitScreen.module.css';

export function CockpitScreen({ competitionId, initial }: { competitionId: string; initial: CockpitState }) {
  const { state, wsStatus } = useArenaStream(competitionId, initial);
  return (
    <div className={styles.cockpit}>
      <RunHeader header={state.header} wsStatus={wsStatus} />
      <ProofTraceStrip trace={state.trace} />
      <div className={styles.grid}>
        <div className={styles.left}>
          <ClvLeaderboard rows={state.leaderboard} />
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
