'use client';
import { useEffect, useState } from 'react';
import { ArenaSocket } from '@/lib/ws';
import { API_BASE } from '@/lib/api';
import type { CanonicalEvent, CockpitState, WsStatus } from '@/lib/contracts';

// Apply one canonical event to the cockpit projection. Pure + exported for reuse.
export function applyEvent(state: CockpitState, event: CanonicalEvent): CockpitState {
  if (state.events.some((e) => e.seq === event.seq)) return state; // dedupe by seq
  return { ...state, events: [event, ...state.events].slice(0, 200) };
}

// Live stream is WS /competitions/{id}/arena; replay/catch-up after a gap is
// GET /competitions/{id}/events?since_seq= (canonical CompetitionEvent[]).
function wsUrl(competitionId: string): string {
  const base = API_BASE || (typeof window !== 'undefined' ? window.location.origin : '');
  return `${base.replace(/^http/, 'ws')}/competitions/${competitionId}/arena`;
}

export function useArenaStream(
  competitionId: string,
  initial: CockpitState,
): { state: CockpitState; wsStatus: WsStatus } {
  const [state, setState] = useState<CockpitState>(initial);
  const [wsStatus, setWsStatus] = useState<WsStatus>('connecting');

  useEffect(() => {
    // Reset to the new competition's snapshot before re-subscribing. The App Router
    // reuses this component on a param-only change (/arena/[a] -> /arena/[b]), and
    // useState keeps the prior state — without this reset, B's events would prepend
    // onto A's stale projection (cross-competition contamination).
    setState(initial);
    setWsStatus('connecting');
    const sock = new ArenaSocket(wsUrl(competitionId), {
      onEvent: (event) => setState((prev) => applyEvent(prev, event)),
      onGap: () => setWsStatus('reconnecting'), // surface; resync via GET /competitions/{id}/events?since_seq=lastSeq
      onStatus: (s) =>
        setWsStatus(s === 'connected' ? 'connected' : s === 'connecting' ? 'connecting' : 'disconnected'),
    });
    sock.connect();
    return () => sock.close();
    // `initial` is intentionally read but not a dep: we reset to whatever snapshot
    // is current at the moment competitionId changes, and re-subscribe only then.
  }, [competitionId]);

  return { state, wsStatus };
}
