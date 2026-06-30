import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/react';
import { LiveDot } from '@/components/ui/LiveDot';

function setReducedMotion(matches: boolean) {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
}

describe('LiveDot', () => {
  it('pulses when motion is allowed', () => {
    setReducedMotion(false);
    const { container } = render(<LiveDot />);
    const dot = container.querySelector('span[data-livedot]') as HTMLElement;
    expect(dot).toBeTruthy();
    expect(dot.className).toMatch(/pulse/);
  });

  it('does not apply the pulse class under prefers-reduced-motion', () => {
    setReducedMotion(true);
    const { container } = render(<LiveDot />);
    const dot = container.querySelector('span[data-livedot]') as HTMLElement;
    expect(dot.className).not.toMatch(/pulse/);
  });
});
