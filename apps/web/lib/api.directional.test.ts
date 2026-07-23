// MAJOR-1 (Gate-3): the directional reader must send the LOWERCASE backend enum on the wire.
// The backend board_kind enum is LOWERCASE (veridex/public_projection.py:108-117 — 'official_benchmark'
// | 'public_agents'); an UPPERCASE value is REJECTED with 422 (probe: board_kind=PUBLIC_AGENTS → 422
// "Input should be 'official_benchmark' or 'public_agents'"; board_kind=public_agents → 200). This test
// does NOT mock the reader — it mocks fetch and pins the exact outgoing query enum value.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { getDirectionalLeaderboard } from '@/lib/api';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('getDirectionalLeaderboard — outgoing board_kind matches the LOWERCASE backend enum', () => {
  it('default board_kind is the lowercase public_agents the backend accepts (never UPPERCASE → 422)', async () => {
    const fetchMock = vi.fn(async (_url: unknown) =>
      new Response(JSON.stringify({ board_kind: 'public_agents', rows: [] }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await getDirectionalLeaderboard();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('board_kind=public_agents'); // lowercase — the value the backend accepts
    expect(url).not.toContain('PUBLIC_AGENTS');         // never the 422-rejected uppercase literal
  });

  it('official_benchmark board_kind is forwarded verbatim (lowercase closed enum)', async () => {
    const fetchMock = vi.fn(async (_url: unknown) =>
      new Response(JSON.stringify({ board_kind: 'official_benchmark', rows: [] }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await getDirectionalLeaderboard('official_benchmark');
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('board_kind=official_benchmark');
  });
});
