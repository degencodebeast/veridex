import { describe, it, expect, vi, afterEach } from 'vitest';
import { render } from '@testing-library/react';

const captured: { initialFixtureId?: number; packId?: string }[] = [];
vi.mock('@/components/screens/CreateCompetitionScreen', () => ({
  CreateCompetitionScreen: (props: { initialFixtureId?: number; packId?: string }) => {
    captured.push({ initialFixtureId: props.initialFixtureId, packId: props.packId });
    return null;
  },
}));
const searchParams = { current: new URLSearchParams() };
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => searchParams.current,
}));

import CreateCompetitionPage from './page';

afterEach(() => { captured.length = 0; });

describe('competitions/create page — parses pack_id + fixture_id from the query', () => {
  it('forwards the authoritative pack_id + fixture_id (new params) into the wizard', () => {
    searchParams.current = new URLSearchParams('pack_id=curated&fixture_id=18209181');
    render(<CreateCompetitionPage />);
    expect(captured.at(-1)).toEqual({ initialFixtureId: 18209181, packId: 'curated' });
  });

  it('still honors the legacy ?fixture= param (no pack_id)', () => {
    searchParams.current = new URLSearchParams('fixture=18172280');
    render(<CreateCompetitionPage />);
    expect(captured.at(-1)).toEqual({ initialFixtureId: 18172280, packId: undefined });
  });
});
