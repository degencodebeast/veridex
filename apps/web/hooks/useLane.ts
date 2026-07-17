'use client';
import { useEffect, useState } from 'react';

export type Lane = 'directional' | 'maker';

// Maker Arena lane switch (MM-R1) — shared by Leaderboard/Agents/Duel. URL-addressable
// (`?lane=maker`) so a maker view can be linked/bookmarked directly; reads `window.location`
// on mount rather than next/navigation's useSearchParams so callers don't need a Suspense
// boundary (same lightweight pattern lib/mock.ts already uses for `?mock=1`).
export function useLane(): [Lane, (l: Lane) => void] {
  const [lane, setLaneState] = useState<Lane>('directional');

  useEffect(() => {
    const p = new URLSearchParams(window.location.search).get('lane');
    if (p === 'maker') setLaneState('maker');
  }, []);

  function setLane(l: Lane) {
    setLaneState(l);
    const url = new URL(window.location.href);
    if (l === 'maker') url.searchParams.set('lane', 'maker');
    else url.searchParams.delete('lane');
    window.history.replaceState({}, '', url);
  }

  return [lane, setLane];
}
