import { test, expect } from '@playwright/test';

test('landing renders the product promise', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await expect(page.getByText(/cannot self-certify/i)).toBeVisible();
});

test('top nav exposes the five public sections', async ({ page }) => {
  await page.goto('/');
  // Scope to the Primary nav: the landing CTA "Enter the Arena →" also matches
  // a substring "Arena" link, so an unscoped query is ambiguous (strict mode).
  const nav = page.getByRole('navigation', { name: 'Primary' });
  for (const label of ['Competitions', 'Arena', 'Markets', 'Leaderboard', 'Agents']) {
    await expect(nav.getByRole('link', { name: label })).toBeVisible();
  }
});
