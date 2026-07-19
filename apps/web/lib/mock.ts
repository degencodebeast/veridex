// Frontend-only MOCK MODE (DEMO data) — lets the full UI be inspected before real backend
// data flows. OFF by default. When ON, lib/api.ts routes its readers to the EXISTING typed
// wire fixtures (contracts/fixtures/*.json — the same ones the parse tests validate against
// the frozen contract) instead of fetching the backend. No new data is authored here.
//
// HONESTY (doctrine): mock data is DEMO/REPLAY, never LIVE. isMockEnabled() drives a persistent
// "DEMO DATA · MOCK MODE" indicator; the api.ts mock branch demotes any `live` source_mode to
// `replay` so no screen renders a LIVE badge over fixtures. The fixtures' anchor is honestly
// `not_anchored`, so a mock Proof/Verify shows a demo recompute — never a real anchored claim.
//
// SOURCE OF TRUTH: the canonical wire fixtures live at repo-root contracts/fixtures/*.json (the
// frozen contract the parse tests validate against). This app's Docker image builds with
// build.context=./apps/web (see compose.coolify.yml) — a self-sufficient context that cannot reach
// repo-root files — so the fixtures consumed at BUILD time are vendored copies under
// lib/fixtures/wire/. They are NOT re-authored: fixtures/wire/sync.test.ts asserts every copy is
// byte-identical to its repo-root canonical, so the copies fail CI the moment they drift.
import type * as W from '@/lib/wire';
import proofArtifact from './fixtures/wire/proof_artifact.json';
import verifyResponse from './fixtures/wire/verify_response.json';
import leaderboard from './fixtures/wire/leaderboard.json';
import competitionState from './fixtures/wire/competition_state.json';
import inspectorRecord from './fixtures/wire/inspector_record.json';
import feedHealth from './fixtures/wire/feed_health.json';
import makerArenaResult from './fixtures/wire/maker_arena_result.json';

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
  makerArenaResult: makerArenaResult as unknown as W.MakerArenaResultResponseWire,
};
