'use client';
import { useEffect, useRef, useState } from 'react';
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
  const sockRef = useRef<ArenaSocket | null>(null);

  useEffect(() => {
    const sock = new ArenaSocket(wsUrl(competitionId), {
      onEvent: (event) => setState((prev) => applyEvent(prev, event)),
      onGap: () => setWsStatus('reconnecting'), // surface; resync via GET /competitions/{id}/events?since_seq=lastSeq
      onStatus: (s) =>
        setWsStatus(s === 'connected' ? 'connected' : s === 'connecting' ? 'connecting' : 'disconnected'),
    });
    sockRef.current = sock;
    sock.connect();
    return () => sock.close();
  }, [competitionId]);

  return { state, wsStatus };
}
