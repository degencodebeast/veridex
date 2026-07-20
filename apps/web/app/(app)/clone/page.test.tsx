// T-2 remediation · the Clone Preview deep-link must be HONEST off-mock: with the demo flag OFF it
// must NOT fabricate a source agent (the old unconditional `?? AGENT_PROFILES.value_clv` fallback),
// and must render an honest "source unavailable" state instead. With mock ON it serves the labeled
// DEMO source profile. useRouter/useSearchParams are mocked; nothing here touches the network.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AGENT_PROFILES } from '@/lib/fixtures/catalog';

const h = vi.hoisted(() => ({ search: '', push: vi.fn() }));
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: h.push }),
  useSearchParams: () => new URLSearchParams(h.search),
}));

import ClonePreviewPage from './page';

afterEach(() => {
  vi.unstubAllEnvs();
  h.search = '';
  h.push.mockReset();
});

const NAME = AGENT_PROFILES.value_clv.agent_name;

describe('ClonePreviewPage deep-link honesty (T-2)', () => {
  it('mock OFF: renders an honest unavailable state — never the fabricated value_clv source', async () => {
    h.search = 'source=value_clv';
    render(<ClonePreviewPage />);
    expect(await screen.findByText(/unavailable/i)).toBeInTheDocument();
    // the fabricated clone preview (its heading / recompute copy) must NOT appear off-mock
    expect(screen.queryByRole('heading', { name: new RegExp(`Clone ${NAME}`) })).toBeNull();
    expect(screen.queryByText(/the law recomputes your own clv/i)).toBeNull();
  });

  it('mock OFF: even with NO source param, never falls back to a fabricated default agent', async () => {
    h.search = '';
    render(<ClonePreviewPage />);
    expect(await screen.findByText(/unavailable/i)).toBeInTheDocument();
    expect(screen.queryByText(/the law recomputes your own clv/i)).toBeNull();
  });

  it('mock ON: serves the labeled DEMO source profile for the requested source', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    h.search = 'source=value_clv';
    render(<ClonePreviewPage />);
    expect(await screen.findByRole('heading', { name: new RegExp(`Clone ${NAME}`) })).toBeInTheDocument();
    expect(screen.getByText(/the law recomputes your own clv/i)).toBeInTheDocument();
  });
});
