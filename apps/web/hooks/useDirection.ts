'use client';
import { useEffect, useState } from 'react';

type Direction = 'a' | 'b';
const KEY = 'veridex.direction';

export function useDirection(): { direction: Direction; setDirection: (d: Direction) => void } {
  const [direction, setDir] = useState<Direction>('a');

  useEffect(() => {
    const saved = (typeof localStorage !== 'undefined' && localStorage.getItem(KEY)) as Direction | null;
    if (saved === 'a' || saved === 'b') {
      setDir(saved);
      document.documentElement.dataset.direction = saved;
    }
  }, []);

  function setDirection(d: Direction) {
    setDir(d);
    if (typeof localStorage !== 'undefined') localStorage.setItem(KEY, d);
    document.documentElement.dataset.direction = d;
  }

  return { direction, setDirection };
}
