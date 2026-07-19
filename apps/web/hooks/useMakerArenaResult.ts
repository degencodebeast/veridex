'use client';
import { useEffect, useState } from 'react';
import { getMakerArenaResult } from '@/lib/api';
import type { MakerArenaResultView } from '@/lib/contracts';

export type MakerArenaResultState =
  | { status: 'idle'; result: null }
  | { status: 'loading'; result: null }
  | { status: 'unavailable'; result: null }
  | { status: 'ready'; result: MakerArenaResultView };

export function useMakerArenaResult(
  enabled: boolean,
  injectedResult?: MakerArenaResultView,
): MakerArenaResultState {
  const [state, setState] = useState<MakerArenaResultState>(
    injectedResult
      ? { status: 'ready', result: injectedResult }
      : { status: 'idle', result: null },
  );

  useEffect(() => {
    if (injectedResult) {
      setState((current) => current.status === 'ready' && current.result === injectedResult
        ? current
        : { status: 'ready', result: injectedResult });
      return;
    }
    if (!enabled) {
      setState((current) => current.status === 'idle'
        ? current
        : { status: 'idle', result: null });
      return;
    }

    let ignore = false;
    setState({ status: 'loading', result: null });
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

  return state;
}
