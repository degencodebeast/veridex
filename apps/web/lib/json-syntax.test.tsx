import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { renderJson } from '@/lib/json-syntax';

describe('renderJson (GUD-002 syntax coloring from a data object)', () => {
  it('colors keys, strings, numbers, and booleans with token classes', () => {
    const { container } = render(<pre>{renderJson({ side: 'FRA', edge: 22, valid: true })}</pre>);
    expect(container.querySelector('.jsonKey')).toBeTruthy();
    expect(container.querySelector('.jsonString')).toBeTruthy();
    expect(container.querySelector('.jsonNumber')).toBeTruthy();
    expect(container.querySelector('.jsonBool')).toBeTruthy();
  });

  it('renders the data faithfully as text', () => {
    const { container } = render(<pre>{renderJson({ a: 1 })}</pre>);
    expect(container.textContent).toContain('"a"');
    expect(container.textContent).toContain('1');
  });
});
