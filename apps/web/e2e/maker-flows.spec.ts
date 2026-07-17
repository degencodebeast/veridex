import { test, expect } from '@playwright/test';

// Maker Arena (MM-R1) flows — I-R remediation coverage. Run with NEXT_PUBLIC_VERIDEX_MOCK=1 so
// the sealed fixture backs every surface (same canonical invocation as the review verdict).

test('Maker leaderboard lane: PROOF affordance is a real link into the Maker Proof Card', async ({ page }) => {
  await page.goto('/leaderboard?lane=maker');
  await expect(page.getByText(/adverse-selection toxicity/i).first()).toBeVisible();
  await page.getByRole('link', { name: 'Proof card for txline-fair-mm' }).click();
  await expect(page).toHaveURL(/\/proof\/maker\/txline-fair-mm/);
  await expect(page.getByRole('heading', { name: 'Maker Proof Card' })).toBeVisible();
});

test('Maker Proof Card carries the sealed configuration identity (config_hash)', async ({ page }) => {
  await page.goto('/proof/maker/txline-fair-mm');
  const hash = page.getByTestId('maker-proof-config-hash');
  await expect(hash).toHaveText('f997d5…6f33'); // shortHash of the sealed fixture config_hash
  await expect(hash).toHaveAttribute(
    'title',
    'f997d5a8fcb7d7c4cb02048a56bfb7bcdfabc06c6657ea97bf84be43beb16f33',
  );
});

test('Maker duel makes no external-anchor claim and shows per-agent scored counts', async ({ page }) => {
  await page.goto('/duel?lane=maker');
  await expect(page.locator('[data-variant="anchored"]')).toHaveCount(0);
  await expect(page.locator('[data-variant="not-anchored"]').first()).toBeVisible();
  // per-agent scored counts (row.scored), never the fixture-universe n=18
  const scored = page.getByTestId('duel-maker-scored');
  await expect(scored).toHaveCount(2);
  await expect(scored.first()).toContainText('308,826');
});

test('Agents maker lane: PROOF link navigates to the maker proof route', async ({ page }) => {
  await page.goto('/agents?lane=maker');
  await page.getByRole('link', { name: 'Proof card for naive-mm' }).click();
  await expect(page).toHaveURL(/\/proof\/maker\/naive-mm/);
  await expect(page.getByRole('heading', { name: 'Maker Proof Card' })).toBeVisible();
});
