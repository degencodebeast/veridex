'use client';
import { useEffect, useRef, useState } from 'react';
import { ArenaSocket } from '@/lib/ws';
import { API_BASE, getCockpitState } from '@/lib/api';
import type { CanonicalEvent, CockpitState, FeedHealthState, WsStatus } from '@/lib/contracts';

// Apply one canonical event to the cockpit projection. Pure + exported for reuse. A `policy_result`
// event (T10) is ALSO pushed onto `state.policy` so the PolicyDecisions panel updates live —
// on top of appending to the raw event log, never instead of it.
export function applyEvent(state: CockpitState, event: CanonicalEvent): CockpitState {
  if (state.events.some((e) => e.seq === event.seq)) return state; // dedupe by seq
  const events = [event, ...state.events].slice(0, 200);
  const policy = event.policy ? [...state.policy, event.policy] : state.policy;
  return { ...state, events, policy };
}

// Fixed reconnect delay (T10 AC-2D-104). No backoff/jitter — this is a spectator read-only stream
// (never a producer), and the arena route supports gapless `since_seq` replay, so a single steady
// retry cadence is enough; it avoids a hot reconnect loop without the complexity of exponential
// backoff a write-path client would need.
const RECONNECT_DELAY_MS = 1000;

// Live stream is WS /competitions/{id}/arena; a reconnect passes `since_seq` so the server replays
// exactly the gap (never a duplicate, never a re-fabricated skip — CON-002 gapless resync).
function wsUrl(competitionId: string, sinceSeq: number): string {
  const base = API_BASE || (typeof window !== 'undefined' ? window.location.origin : '');
  const url = `${base.replace(/^http/, 'ws')}/competitions/${competitionId}/arena`;
  return sinceSeq > 0 ? `${url}?since_seq=${sinceSeq}` : url;
}

// Honest-empty feed health for when the caller has no REST snapshot yet — never a fabricated
// "healthy/live" default (WD-4 doctrine): connected/ws_live start false, stale starts true.
function emptyFeedHealth(sourceMode: CockpitState['header']['source_mode']): FeedHealthState {
  return {
    source_mode: sourceMode, ws_live: false, connected: false, txline_configured: false,
    events_per_min: null, ticks_seen: 0, staleness_s: null, stale: true, fixture_id: null,
    anchor_status: 'not_applicable', last_tick_ts: null,
  };
}

export function useArenaStream(
  competitionId: string,
  initial: CockpitState,
  initialFeedHealth?: FeedHealthState,
  // Injectable competition-scoped state fetch (GET /competitions/{id}); defaults to the real reader.
  // A SCORE_UPDATE drives a refetch so the cockpit's leaderboard + receipts stay backend-authoritative
  // (F-5) — NEVER the global cross-run /leaderboard, and never a local re-sort.
  fetchState: (id: string) => Promise<CockpitState> = getCockpitState,
): { state: CockpitState; wsStatus: WsStatus; feedHealth: FeedHealthState } {
  const [state, setState] = useState<CockpitState>(initial);
  const [wsStatus, setWsStatus] = useState<WsStatus>('connecting');
  const [feedHealth, setFeedHealth] = useState<FeedHealthState>(
    initialFeedHealth ?? emptyFeedHealth(initial.header.source_mode),
  );
  const lastSeqRef = useRef(0);

  useEffect(() => {
    // Reset to the new competition's snapshot before re-subscribing. The App Router
    // reuses this component on a param-only change (/arena/[a] -> /arena/[b]), and
    // useState keeps the prior state — without this reset, B's events would prepend
    // onto A's stale projection (cross-competition contamination).
    setState(initial);
    setWsStatus('connecting');
    setFeedHealth(initialFeedHealth ?? emptyFeedHealth(initial.header.source_mode));
    lastSeqRef.current = 0;

    let sock: ArenaSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    // Refetch the competition-scoped state and project ONLY the backend-authoritative leaderboard +
    // sealed receipts (the live event stream/policy/trace remain WS-driven, never clobbered). A
    // failed refetch keeps the prior board verbatim — honest: never fabricate or blank on error.
    const refetchBoard = async (): Promise<void> => {
      try {
        const fresh = await fetchState(competitionId);
        if (cancelled) return; // a competition switch tore this subscription down — drop the stale result
        setState((prev) => ({ ...prev, leaderboard: fresh.leaderboard, receipts: fresh.receipts }));
      } catch {
        /* keep the prior projection — no fabrication, no blanking */
      }
    };

    const scheduleReconnect = () => {
      if (cancelled || reconnectTimer !== null) return;
      // Visible + honest: never a frozen stale-as-live view while we wait to resubscribe.
      setWsStatus('reconnecting');
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        if (!cancelled) connect();
      }, RECONNECT_DELAY_MS);
    };

    function connect(): void {
      sock = new ArenaSocket(wsUrl(competitionId, lastSeqRef.current), {
        onEvent: (event) => {
          lastSeqRef.current = event.seq;
          setState((prev) => applyEvent(prev, event));
          if (event.type === 'score_update') {
            // A score landed — the backend leaderboard just changed. Refetch it (competition-scoped,
            // backend-authoritative) rather than re-deriving a rank client-side (CON-203 / SEC-005).
            void refetchBoard();
          }
          if (event.type === 'MARKET_TICK') {
            // A live tick is the freshest possible proof of liveness — clear stale here (never
            // in onStatus('connected'), which fires before any tick has actually been seen).
            // FOLD 1: without this, `stale` only ever gets FORCED true (connecting/disconnected)
            // and never cleared, so a perfectly healthy ticking feed renders "feed stale" forever.
            setFeedHealth((prev) => ({
              ...prev, ticks_seen: prev.ticks_seen + 1, last_tick_ts: event.ts, stale: false,
            }));
          }
        },
        // A sequence gap or slow-client overflow closes the socket (CON-002) — resync via a fresh
        // subscription from lastSeq rather than leaving the spectator silently stuck.
        onGap: scheduleReconnect,
        onStatus: (s) => {
          const mapped: WsStatus = s === 'connected' ? 'connected' : s === 'connecting' ? 'connecting' : 'disconnected';
          setWsStatus(mapped);
          setFeedHealth((prev) => ({
            ...prev,
            connected: mapped === 'connected',
            ws_live: mapped === 'connected',
            // Honesty: only a connected socket can vouch the feed is fresh — never claim
            // freshness while disconnected/reconnecting (no frozen stale-as-live view).
            stale: mapped === 'connected' ? prev.stale : true,
          }));
          if (mapped === 'disconnected') scheduleReconnect();
        },
      });
      sock.connect();
    }

    connect();
    return () => {
      cancelled = true;
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      sock?.close();
    };
    // `initial`/`initialFeedHealth` are intentionally read but not deps: we reset to whatever
    // snapshot is current at the moment competitionId changes, and re-subscribe only then.
  }, [competitionId]);

  return { state, wsStatus, feedHealth };
}
