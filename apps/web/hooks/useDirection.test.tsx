import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useDirection } from '@/hooks/useDirection';

beforeEach(() => { localStorage.clear(); document.documentElement.removeAttribute('data-direction'); });

describe('useDirection (CON-001)', () => {
  it('defaults to direction A and flips to B, setting the html attribute', () => {
    const { result } = renderHook(() => useDirection());
    expect(result.current.direction).toBe('a');
    act(() => result.current.setDirection('b'));
    expect(result.current.direction).toBe('b');
    expect(document.documentElement.getAttribute('data-direction')).toBe('b');
    expect(localStorage.getItem('veridex.direction')).toBe('b');
  });
});
