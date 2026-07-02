'use client';
import { useReducedMotion } from './useReducedMotion';
import { PULSE_DURATION_MS } from '@/lib/motion';

export function usePulse(): { pulsing: boolean; durationMs: number } {
  const reduced = useReducedMotion();
  return { pulsing: !reduced, durationMs: PULSE_DURATION_MS };
}
