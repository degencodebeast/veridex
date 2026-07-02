import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { JsonView } from '@/components/ui/JsonView';

describe('JsonView (GUD-002)', () => {
  it('renders keys, strings, numbers and bools from a data object (read-only)', () => {
    const { container } = render(<JsonView data={{ market_key: '1X2', edge_bps: 14, valid: true }} />);
    expect(container.querySelector('pre')).toBeTruthy();
    expect(container.textContent).toContain('market_key');
    expect(container.textContent).toContain('14');
    expect(container.textContent).toContain('true');
    expect(container.querySelectorAll('input, textarea, [contenteditable="true"]').length).toBe(0);
  });
});
