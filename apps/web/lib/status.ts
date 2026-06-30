import type { SourceMode, ExecutionMode, WsStatus } from '@/lib/contracts';

// The frontend's expected verifier version — a static build fact, always shown in the status
// bar (matches the fixtures' verifier_version). Single source; bump when the verifier moves.
export const VERIFIER_VERSION = 'v0.9.2';

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
}
