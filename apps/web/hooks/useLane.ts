'use client';
import { useEffect, useState } from 'react';

export type Lane = 'directional' | 'maker';

// Maker Arena lane switch (MM-R1) — shared by Leaderboard/Agents/Duel. URL-addressable
// (`?lane=maker`) so a maker view can be linked/bookmarked directly; reads `window.location`
// on mount rather than next/navigation's useSearchParams so callers don't need a Suspense
// boundary (same lightweight pattern lib/mock.ts already uses for `?mock=1`).
// Returns [lane, setLane, resolved]. `resolved` is false until the mount effect has read `?lane` from
// the URL, then true — so a consumer that owns a lane-conditional side effect (E4's Duel roster read)
// can WAIT for the real URL lane before acting, instead of firing on the pre-hydration default lane.
// The 3rd tuple element is additive: existing `[lane, setLane]` callers stay valid.
export function useLane(): [Lane, (l: Lane) => void, boolean] {
  const [lane, setLaneState] = useState<Lane>('directional');
  const [resolved, setResolved] = useState(false);

  useEffect(() => {
    const p = new URLSearchParams(window.location.search).get('lane');
    if (p === 'maker') setLaneState('maker');
    setResolved(true);
  }, []);

  function setLane(l: Lane) {
    setLaneState(l);
    const url = new URL(window.location.href);
    if (l === 'maker') url.searchParams.set('lane', 'maker');
    else url.searchParams.delete('lane');
    window.history.replaceState({}, '', url);
  }

  return [lane, setLane, resolved];
}
