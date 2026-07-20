'use client';
import { useEffect, useState } from 'react';
import { getMakerArenaResult } from '@/lib/api';
import type { MakerArenaResultView } from '@/lib/contracts';

export type MakerArenaResultState =
  | { status: 'idle'; result: null }
  | { status: 'loading'; result: null }
  | { status: 'unavailable'; result: null }
  | { status: 'ready'; result: MakerArenaResultView };

const IDLE_STATE: MakerArenaResultState = { status: 'idle', result: null };
const LOADING_STATE: MakerArenaResultState = { status: 'loading', result: null };

export function useMakerArenaResult(
  enabled: boolean,
  injectedResult?: MakerArenaResultView,
): MakerArenaResultState {
  const [state, setState] = useState<MakerArenaResultState>(IDLE_STATE);

  useEffect(() => {
    if (injectedResult) {
      setState((current) => current.status === 'idle' ? current : IDLE_STATE);
      return;
    }
    if (!enabled) {
      setState((current) => current.status === 'idle' ? current : IDLE_STATE);
      return;
    }

    let ignore = false;
    setState(LOADING_STATE);
    getMakerArenaResult().then(
      (result) => {
        if (!ignore) setState({ status: 'ready', result });
      },
      () => {
        if (!ignore) setState({ status: 'unavailable', result: null });
      },
    );

    return () => {
      ignore = true;
    };
  }, [enabled, injectedResult]);

  if (injectedResult) return { status: 'ready', result: injectedResult };
  if (!enabled) return IDLE_STATE;
  if (state.status === 'idle') return LOADING_STATE;
  return state;
}
