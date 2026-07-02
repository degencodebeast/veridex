import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MockBanner } from '@/components/layout/MockBanner';

afterEach(() => { vi.unstubAllEnvs(); window.history.replaceState(null, '', '/'); });

describe('MockBanner (honest DEMO indicator)', () => {
  it('shows a persistent DEMO DATA · MOCK MODE strip when mock is on', () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    render(<MockBanner />);
    const banner = screen.getByTestId('mock-banner');
    expect(banner).toHaveTextContent(/DEMO DATA · MOCK MODE/i);
    expect(banner).toHaveTextContent(/never live/i); // honest: replay, not live
  });

  it('renders nothing when mock is off (default)', () => {
    const { container } = render(<MockBanner />);
    expect(container.firstChild).toBeNull();
  });
});
