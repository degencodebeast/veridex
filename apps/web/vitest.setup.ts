import '@testing-library/jest-dom/vitest';
import { vi, afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

afterEach(() => cleanup());

// jsdom has no matchMedia; default to "motion allowed". Tests override as needed.
if (!window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }),
  });
}

// next/font/google is a build-time transform that throws under Vitest — stub it.
vi.mock('next/font/google', () => ({
  IBM_Plex_Sans: () => ({ variable: '--font-sans', className: 'font-sans' }),
  IBM_Plex_Mono: () => ({ variable: '--font-mono', className: 'font-mono' }),
}));

// next/font/local (used by app/layout.tsx for hermetic self-hosted fonts) is likewise a
// build-time transform that throws under Vitest — stub the default export.
vi.mock('next/font/local', () => ({
  default: () => ({ variable: '--font-local', className: 'font-local' }),
}));

// next/navigation: default usePathname; individual tests can re-mock.
vi.mock('next/navigation', () => ({
  usePathname: () => '/',
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
