import { describe, it, expect, vi, afterEach } from 'vitest';
import { isMockEnabled } from '@/lib/mock';

afterEach(() => {
  vi.unstubAllEnvs();
  window.history.replaceState(null, '', '/'); // reset any ?mock= query
});

describe('isMockEnabled (frontend mock flag)', () => {
  it('is OFF by default — env unset, no query', () => {
    expect(isMockEnabled()).toBe(false);
  });

  it('is ON via the NEXT_PUBLIC_VERIDEX_MOCK env flag', () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    expect(isMockEnabled()).toBe(true);
  });

  it('is ON via the ?mock=1 per-tab query param', () => {
    window.history.replaceState(null, '', '/?mock=1');
    expect(isMockEnabled()).toBe(true);
  });
});
