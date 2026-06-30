'use client';
import { SegmentedControl } from './SegmentedControl';
import { useDirection } from '@/hooks/useDirection';

export function DirectionToggle() {
  const { direction, setDirection } = useDirection();
  return (
    <SegmentedControl<'a' | 'b'>
      ariaLabel="Visual direction"
      value={direction}
      onChange={setDirection}
      options={[{ value: 'a', label: 'A · Terminal' }, { value: 'b', label: 'B · SaaS' }]}
    />
  );
}
