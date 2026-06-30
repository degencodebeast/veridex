// Frontend-only MOCK MODE (DEMO data) — lets the full UI be inspected before real backend
// data flows. OFF by default. When ON, lib/api.ts routes its readers to the EXISTING typed
// wire fixtures (contracts/fixtures/*.json — the same ones the parse tests validate against
// the frozen contract) instead of fetching the backend. No new data is authored here.
//
// HONESTY (doctrine): mock data is DEMO/REPLAY, never LIVE. isMockEnabled() drives a persistent
// "DEMO DATA · MOCK MODE" indicator; the api.ts mock branch demotes any `live` source_mode to
// `replay` so no screen renders a LIVE badge over fixtures. The fixtures' anchor is honestly
// `not_anchored`, so a mock Proof/Verify shows a demo recompute — never a real anchored claim.
import type * as W from '@/lib/wire';
import proofArtifact from '../../../contracts/fixtures/proof_artifact.json';
import verifyResponse from '../../../contracts/fixtures/verify_response.json';
import leaderboard from '../../../contracts/fixtures/leaderboard.json';
import competitionState from '../../../contracts/fixtures/competition_state.json';
import inspectorRecord from '../../../contracts/fixtures/inspector_record.json';
import feedHealth from '../../../contracts/fixtures/feed_health.json';

/** True when the frontend mock flag is set: env (build/runtime) or the `?mock=1` per-tab dev param. */
export function isMockEnabled(): boolean {
  const env = process.env.NEXT_PUBLIC_VERIDEX_MOCK;
  if (env === '1' || env === 'true') return true;
  if (typeof window !== 'undefined') {
    const p = new URLSearchParams(window.location.search).get('mock');
    if (p === '1' || p === 'true') return true;
  }
  return false;
}

// The canonical wire fixtures, typed to the frozen wire contract (no duplication / re-authoring).
export const MOCK_FIXTURES = {
  proofArtifact: proofArtifact as unknown as W.ProofArtifact,
  verify: verifyResponse as unknown as W.VerifyResult,
  leaderboard: leaderboard as unknown as W.LeaderboardResponse,
  competition: competitionState as unknown as W.CompetitionStateResponse,
  inspector: inspectorRecord as unknown as W.InspectorRecord,
  feedHealth: feedHealth as unknown as W.FeedHealth,
};
