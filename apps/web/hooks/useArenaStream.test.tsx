import { describe, it, expect, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useArenaStream } from '@/hooks/useArenaStream';
import { sampleCockpitState } from '@/__tests__/fixtures/contracts';
import type { CockpitState } from '@/lib/contracts';

// Stub ArenaSocket so the hook never opens a real WebSocket.
vi.mock('@/lib/ws', () => ({
  ArenaSocket: class {
    constructor() {}
    connect() {}
    close() {}
  },
}));

function stateFor(id: string, seqs: number[]): CockpitState {
  return {
    ...sampleCockpitState,
    competition_id: id,
    events: seqs.map((seq) => ({ seq, type: 'AGENT_ACTION', payload_hash: '0x', evidence: true, ts: seq })),
  };
}

describe('useArenaStream (cross-competition isolation)', () => {
  it('resets the projection when competitionId changes (no stale-event contamination)', () => {
    const a = stateFor('comp-a', [10, 11, 12]);
    const b = stateFor('comp-b', []);
    const { result, rerender } = renderHook(
      ({ id, init }) => useArenaStream(id, init),
      { initialProps: { id: 'comp-a', init: a } },
    );
    expect(result.current.state.competition_id).toBe('comp-a');
    expect(result.current.state.events.map((e) => e.seq)).toEqual([10, 11, 12]);

    // Navigate /arena/comp-a -> /arena/comp-b (App Router reuses the component).
    rerender({ id: 'comp-b', init: b });
    expect(result.current.state.competition_id).toBe('comp-b'); // reset, not stale comp-a
    expect(result.current.state.events).toEqual([]); // comp-a's events must not carry over
  });
});
