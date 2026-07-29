import { test, expect } from '@playwright/test'

test('redirects unauthenticated users to login', async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveURL(/\/login/)
  await expect(page.getByText('ForgeAI').first()).toBeVisible()
  await expect(page.getByRole('heading', { name: '登录工作台' })).toBeVisible()
})
