// I-5 — the API client must resolve SSR fetches against the configured ABSOLUTE base (a relative
// URL cannot be fetched during server rendering), and FAIL LOUD when the base is missing on the
// server rather than silently hitting the wrong origin. Browser (same-origin) calls stay relative.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { getLeaderboard } from '@/lib/api';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

function stubFetch(): ReturnType<typeof vi.fn> {
  const leaderboard = { rows: [] };
  const fn = vi.fn(async () => new Response(JSON.stringify(leaderboard), { status: 200 }));
  vi.stubGlobal('fetch', fn as unknown as typeof fetch);
  return fn;
}

describe('SSR URL resolution (I-5)', () => {
  it('resolves a reader fetch against the configured ABSOLUTE base', async () => {
    vi.stubEnv('NEXT_PUBLIC_API_BASE', 'https://api.example.test');
    const fetchMock = stubFetch();
    await getLeaderboard();
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toBe('https://api.example.test/leaderboard');
    expect(/^https?:\/\//.test(url)).toBe(true); // absolute, never a bare relative path
  });

  it('throws a loud error on the server when NEXT_PUBLIC_API_BASE is missing', async () => {
    vi.stubEnv('NEXT_PUBLIC_API_BASE', '');
    vi.stubGlobal('window', undefined); // simulate the server (SSR) context
    stubFetch();
    await expect(getLeaderboard()).rejects.toThrow(/NEXT_PUBLIC_API_BASE/);
  });
});
