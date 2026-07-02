import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { InspectorScreen } from '@/components/screens/inspector/InspectorScreen';
import { PerformanceMetricsBlock } from '@/components/screens/proof/PerformanceMetricsBlock';
import { sampleInspectorRecord, sampleProofArtifact } from '@/__tests__/fixtures/contracts';

beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
});

describe('strategy-doctrine copy wired into screens', () => {
  it('Inspector separates Fair Value, Executable Edge, CLV, Stake and never calls Stable Price true probability', () => {
    render(<InspectorScreen record={sampleInspectorRecord} />);
    const clv = screen.getByLabelText('CLV explanation');
    expect(within(clv).getByText('Fair Value')).toBeInTheDocument();
    expect(within(clv).getByText('Executable Edge')).toBeInTheDocument();
    expect(within(clv).getByText('CLV')).toBeInTheDocument();
    expect(within(clv).getByText('Stake · Kelly')).toBeInTheDocument();
    expect(screen.getByText(/not a guaranteed true probability/i)).toBeInTheDocument();
  });

  it('Performance Metrics footer distinguishes CLV (ranked skill) from Executable Edge', () => {
    render(<PerformanceMetricsBlock metrics={sampleProofArtifact.metrics} />);
    expect(screen.getByText(/Rank is Avg CLV only/i)).toBeInTheDocument();
    expect(screen.getByText(/Executable Edge/i)).toBeInTheDocument();
  });
});
