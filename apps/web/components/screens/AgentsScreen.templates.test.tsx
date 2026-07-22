import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { AgentsScreen } from '@/components/screens/AgentsScreen';

describe('AgentsScreen — Strategy Templates section', () => {
  it('renders label + archetype + readiness for every template, with NO displayed performance', () => {
    render(<AgentsScreen agents={[]} />);
    const section = screen.getByTestId('strategy-templates');

    // Spec §5.3: each template displays label, archetype (taxonomy), blurb, AND readiness.
    expect(within(section).getByText('Value-vs-Venue')).toBeTruthy();
    // The archetype taxonomy IS displayed — `value_clv` is an identifier, not a performance claim.
    expect(within(section).getByTestId('archetype-value_vs_venue').textContent).toContain('value_clv');
    expect(within(section).getByTestId('readiness-value_vs_venue').textContent).toContain('Locked');
    expect(within(section).getByTestId('readiness-quoteguard_mm').textContent).toContain('Deployable');
    expect(within(section).getByTestId('readiness-llm_drift').textContent).toContain('Arena-only');

    // A template must never display a PERFORMANCE VALUE or RANK (§5.3 / §3 boundary 2): no numeric
    // `bps` figure, no avg-CLV NUMBER, no rank/position, no scored metric. The mere `CLV` taxonomy
    // token (in the `value_clv` archetype) is allowed — only a value/number attached to it is not.
    const text = section.textContent ?? '';
    expect(text).not.toMatch(/\d+(?:\.\d+)?\s*bps/i);   // e.g. "129 bps"
    expect(text).not.toMatch(/CLV[\s:]*[+-]?\d/i);      // e.g. "CLV +43", "avg CLV 12"
    expect(text).not.toMatch(/\brank\b\s*#?\s*\d/i);    // e.g. "rank 1", "rank #2"
    expect(text).not.toMatch(/#\d+\b/);                 // e.g. "#1" leaderboard position
  });
});
