import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { PerformanceMetricsBlock } from '@/components/screens/proof/PerformanceMetricsBlock';
import { sampleProofArtifact } from '@/__tests__/fixtures/contracts';

describe('PerformanceMetricsBlock (SEC-001 / REQ-020)', () => {
  it('renders CLV (ranked) and proxy-marked metrics separate from checks', () => {
    render(<PerformanceMetricsBlock metrics={sampleProofArtifact.metrics} />);
    const block = screen.getByLabelText('Performance Metrics');
    // anchor to the metric label (the footer also says "Rank is Avg CLV only").
    expect(within(block).getByText(/^CLV/)).toBeInTheDocument();
    expect(within(block).getByText(/ranked/i)).toBeInTheDocument();
    // anchor to the metric labels (the footer also names "Sim PnL & Brier").
    expect(within(block).getByText(/^Sim PnL/)).toHaveTextContent('ⓟ');
    expect(within(block).getByText(/^Brier/)).toHaveTextContent('ⓟ');
  });

  it('states rank is Avg CLV only', () => {
    render(<PerformanceMetricsBlock metrics={sampleProofArtifact.metrics} />);
    expect(screen.getByText(/Rank is Avg CLV only/i)).toBeInTheDocument();
  });
});
