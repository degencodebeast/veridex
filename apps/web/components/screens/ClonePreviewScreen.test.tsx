import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ClonePreviewScreen } from '@/components/screens/ClonePreviewScreen';
import { AGENT_PROFILES } from '@/lib/fixtures/catalog';

const source = AGENT_PROFILES.value_clv;

describe('ClonePreviewScreen (REQ-022 / AC-023)', () => {
  it('copies config, not the source CLV, and says the law recomputes your own', () => {
    render(<ClonePreviewScreen source={source} />);
    expect(screen.getByText(/the law recomputes your own clv/i)).toBeInTheDocument();
    // the source avg_clv must NOT be presented as the clone's record
    expect(screen.queryByText(new RegExp(`\\+${source.avg_clv_bps}`))).toBeNull();
    expect(screen.getByText(new RegExp(source.config_hash))).toBeInTheDocument();
  });

  it('copies CONFIG ONLY and explicitly discloses what is NOT inherited (#3 / AC-023)', () => {
    render(<ClonePreviewScreen source={source} />);
    // config IS copied: archetype/mode/policy shown in the copied-config view
    expect(screen.getByText(/archetype/)).toBeInTheDocument(); // JsonView key
    expect(screen.getByText(new RegExp(source.policy_hash))).toBeInTheDocument();
    // the preview EXPLICITLY discloses what is NOT copied (the original's CLV/record/identity)
    const notCopied = screen.getByTestId('not-copied');
    expect(within(notCopied).getByText(/avg clv|record/i)).toBeInTheDocument();
    // never claims inheritance; never carries the source's CLV/total into the clone
    expect(screen.queryByText(/inherit/i)).toBeNull();
    expect(screen.queryByText(new RegExp(`\\+${source.total_clv_bps}`))).toBeNull();
  });

  it('shows the clone-cap and a back-to-source link', () => {
    render(<ClonePreviewScreen source={source} />);
    expect(screen.getByText(/clone[- ]cap/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /source profile/i })).toHaveAttribute('href', '/agents/value_clv');
  });

  it('commits a clone via the preview', async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    render(<ClonePreviewScreen source={source} onCommit={onCommit} />);
    await user.click(screen.getByRole('button', { name: /clone into my roster/i }));
    expect(onCommit).toHaveBeenCalled();
  });
});
