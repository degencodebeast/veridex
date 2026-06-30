import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SegmentedControl } from '@/components/ui/SegmentedControl';

describe('SegmentedControl', () => {
  it('marks the active option and fires onChange for an unlocked option', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <SegmentedControl
        ariaLabel="Source"
        value="ALL"
        onChange={onChange}
        options={[{ value: 'ALL', label: 'ALL' }, { value: 'LIVE', label: 'LIVE' }]}
      />,
    );
    expect(screen.getByRole('radio', { name: 'ALL' })).toHaveAttribute('aria-checked', 'true');
    await user.click(screen.getByRole('radio', { name: 'LIVE' }));
    expect(onChange).toHaveBeenCalledWith('LIVE');
  });

  it('does not fire onChange for a locked option and marks it disabled', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <SegmentedControl
        ariaLabel="Mode"
        value="numeric"
        onChange={onChange}
        options={[{ value: 'llm', label: 'LLM', locked: true }, { value: 'numeric', label: 'Numeric' }]}
      />,
    );
    const llm = screen.getByRole('radio', { name: /LLM/ });
    expect(llm).toHaveAttribute('aria-disabled', 'true');
    await user.click(llm);
    expect(onChange).not.toHaveBeenCalled();
  });
});
