import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useAgentOps } from '@/components/ops/useAgentOps';

describe('useAgentOps', () => {
  it('opens for an agent and closes', () => {
    const { result } = renderHook(() => useAgentOps());
    expect(result.current.isOpen).toBe(false);
    act(() => result.current.open('momentum_fr'));
    expect(result.current.isOpen).toBe(true);
    expect(result.current.agentId).toBe('momentum_fr');
    act(() => result.current.close());
    expect(result.current.isOpen).toBe(false);
  });
});
