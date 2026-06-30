import { describe, it, expect, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useReducedMotion } from '@/hooks/useReducedMotion';

function mockMatchMedia(matches: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

// A controllable MQL whose addEventListener captures the 'change' handler, so
// tests can fire a live change and assert cleanup on unmount.
function controllableMatchMedia(initial: boolean) {
  let changeHandler: ((e: MediaQueryListEvent) => void) | null = null;
  const mql = {
    matches: initial,
    media: '(prefers-reduced-motion: reduce)',
    onchange: null,
    addEventListener: vi.fn((event: string, cb: EventListenerOrEventListenerObject) => {
      if (event === 'change') changeHandler = cb as (e: MediaQueryListEvent) => void;
    }),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  };
  window.matchMedia = vi.fn().mockReturnValue(mql);
  return {
    mql,
    fireChange(matches: boolean) {
      mql.matches = matches;
      changeHandler?.({ matches } as MediaQueryListEvent);
    },
  };
}

describe('useReducedMotion', () => {
  it('returns false when motion is allowed', () => {
    mockMatchMedia(false);
    const { result } = renderHook(() => useReducedMotion());
    expect(result.current).toBe(false);
  });

  it('returns true when the user prefers reduced motion', () => {
    mockMatchMedia(true);
    const { result } = renderHook(() => useReducedMotion());
    expect(result.current).toBe(true);
  });

  it('updates live when the media query changes', () => {
    const { fireChange } = controllableMatchMedia(false);
    const { result } = renderHook(() => useReducedMotion());
    expect(result.current).toBe(false);

    act(() => fireChange(true));
    expect(result.current).toBe(true);

    act(() => fireChange(false));
    expect(result.current).toBe(false);
  });

  it('removes the change listener on unmount', () => {
    const { mql } = controllableMatchMedia(false);
    const { unmount } = renderHook(() => useReducedMotion());
    expect(mql.addEventListener).toHaveBeenCalledWith('change', expect.any(Function));

    unmount();
    expect(mql.removeEventListener).toHaveBeenCalledWith('change', expect.any(Function));
  });
});
