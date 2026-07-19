import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useMakerArenaResult } from '@/hooks/useMakerArenaResult';
import { getMakerArenaResult } from '@/lib/api';
import { MAKER_ARENA_RESULT } from '@/lib/fixtures/maker';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/api')>(),
  getMakerArenaResult: vi.fn(),
}));

const getMakerArenaResultMock = vi.mocked(getMakerArenaResult);

describe('useMakerArenaResult (F-9)', () => {
  beforeEach(() => {
    getMakerArenaResultMock.mockReset();
  });

  it('stays idle until enabled, then loads exactly once', async () => {
    getMakerArenaResultMock.mockResolvedValue(MAKER_ARENA_RESULT);
    const { result, rerender } = renderHook(
      ({ enabled }) => useMakerArenaResult(enabled),
      { initialProps: { enabled: false } },
    );

    expect(result.current.status).toBe('idle');
    expect(getMakerArenaResultMock).not.toHaveBeenCalled();

    rerender({ enabled: true });
    expect(result.current.status).toBe('loading');
    rerender({ enabled: true });

    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(result.current.result).toBe(MAKER_ARENA_RESULT);
    expect(getMakerArenaResultMock).toHaveBeenCalledTimes(1);
  });

  it('uses injected data immediately without requesting the API', () => {
    const { result } = renderHook(() => useMakerArenaResult(true, MAKER_ARENA_RESULT));

    expect(result.current).toEqual({ status: 'ready', result: MAKER_ARENA_RESULT });
    expect(getMakerArenaResultMock).not.toHaveBeenCalled();
  });

  it('ignores a stale response after Maker mode closes', async () => {
    let resolveRequest!: (value: typeof MAKER_ARENA_RESULT) => void;
    getMakerArenaResultMock.mockReturnValue(new Promise((resolve) => {
      resolveRequest = resolve;
    }));
    const { result, rerender } = renderHook(
      ({ enabled }) => useMakerArenaResult(enabled),
      { initialProps: { enabled: true } },
    );

    expect(result.current.status).toBe('loading');
    rerender({ enabled: false });
    expect(result.current.status).toBe('idle');

    await act(async () => {
      resolveRequest(MAKER_ARENA_RESULT);
      await Promise.resolve();
    });

    expect(result.current.status).toBe('idle');
  });
});
