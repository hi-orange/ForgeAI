import { test, expect } from '@playwright/test'

test('redirects unauthenticated users to login', async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveURL(/\/login/)
  await expect(page.getByText('Forge').first()).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible()
})
