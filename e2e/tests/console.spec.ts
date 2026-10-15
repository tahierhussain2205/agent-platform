import { expect, test } from '@playwright/test'

test('console loads without errors', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (err) => errors.push(err.message))
  page.on('console', (msg) => msg.type() === 'error' && errors.push(msg.text()))

  const response = await page.goto('/')
  expect(response?.ok()).toBe(true)
  await expect(page).toHaveTitle('Agent Platform Console')
  await expect(page.getByRole('heading', { name: 'Agent Platform' })).toBeVisible()
  expect(errors).toEqual([])
})
