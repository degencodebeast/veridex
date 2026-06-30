import type { SourceMode, ExecutionMode, WsStatus } from '@/lib/contracts';

// Canonical verifier version — the backend's `VERIFIER_VERSION` (veridex/verifier/proof_card.py)
// and the fixtures' `proof_card.verifier_version`. The ONLY hardcode, used as the idle/no-data
// fallback; whenever there's an active run the bar derives the verifier from the artifact instead,
// so it can never contradict the Proof Card. Bump in lockstep with the backend when it moves.
export const VERIFIER_VERSION = 'v0';

// What the shared status bar shows when there IS an active competition/stream. The source bar
// publishes this from the Arena/Cockpit screen; absent (idle) ⇒ honest neutral, nothing faked.
export interface StatusBarState {
  fixture: string;
  competition: string;
  sourceMode: SourceMode;     // REPLAY/LIVE — the honesty-gated data axis (mock ⇒ replay, never fake live)
  executionMode: ExecutionMode;
  ws: WsStatus;               // honest WS state — CONNECTED·seq only when truly connected
  seq: number | null;
  scoring: boolean;
  verifierVersion: string;    // derived from the run's proof artifact ⇒ always === the Proof Card
}
