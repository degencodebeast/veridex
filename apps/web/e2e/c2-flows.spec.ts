import { test, expect } from '@playwright/test';

test('Operator Dashboard opens the read-only Ops drawer', async ({ page }) => {
  await page.goto('/dashboard');
  await page.getByRole('button', { name: /runtime/i }).first().click();
  await expect(page.getByText(/RUNTIME OBSERVABILITY · READ-ONLY · NOT SCORED/i)).toBeVisible();
});

test('Agent Profile → Clone Preview → commit lands on Dashboard', async ({ page }) => {
  await page.goto('/agents/value_clv');
  await page.getByRole('link', { name: /clone this agent/i }).click();
  await expect(page).toHaveURL(/\/clone\?source=value_clv/);
  await expect(page.getByText(/the law recomputes your own clv/i)).toBeVisible();
  await page.getByRole('button', { name: /clone into my roster/i }).click();
  await expect(page).toHaveURL(/\/dashboard/);
});

test('Agents → Compare Two → Duel', async ({ page }) => {
  await page.goto('/agents');
  await page.getByRole('link', { name: /compare two/i }).click();
  await expect(page).toHaveURL(/\/duel/);
  await expect(page.getByText(/same sealed evidence/i)).toBeVisible();
});

test('Mobile Arena renders at 392px with a bottom tab bar', async ({ page }) => {
  await page.setViewportSize({ width: 392, height: 800 });
  await page.goto('/m/arena');
  await expect(page.getByTestId('bottom-tabs')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Rank' })).toBeVisible();
});
