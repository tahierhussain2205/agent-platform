import { expect, type Browser, type Page } from '@playwright/test'

export const PASSWORD = 'password123'

export const uid = () => Math.random().toString(36).slice(2, 8)

/** Collects uncaught page errors; assert it stays empty at the end of a test. */
export function trackErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('pageerror', (err) => errors.push(err.message))
  return errors
}

export async function signIn(page: Page, email: string, password = PASSWORD) {
  await page.goto('/sign-in')
  await page.getByLabel('Email').fill(email)
  await page.getByLabel('Password').fill(password)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByTestId('current-user')).toHaveText(email)
}

export async function signUp(page: Page, email: string, first = 'E2E', last = 'Person') {
  await page.goto('/sign-up')
  await page.getByLabel('First name').fill(first)
  await page.getByLabel('Last name').fill(last)
  await page.getByLabel('Email').fill(email)
  await page.getByLabel('Password').fill(PASSWORD)
  await page.getByRole('button', { name: 'Sign up' }).click()
}

export async function createOrg(page: Page, name: string) {
  await expect(page.getByRole('heading', { name: 'Set up your organization' })).toBeVisible()
  await page.getByLabel('Organization name').fill(name)
  await page.getByRole('button', { name: 'Create organization' }).click()
  await expect(page.getByRole('heading', { level: 1, name })).toBeVisible({ timeout: 20_000 })
}

/** A fresh signed in founder with their own org, in its own browser context. */
export async function founder(browser: Browser, orgName: string) {
  const context = await browser.newContext()
  const page = await context.newPage()
  const email = `founder-${uid()}@e2e.test`
  await signUp(page, email, 'Fran', 'Founder')
  await createOrg(page, orgName)
  return { context, page, email }
}

export function memberRow(page: Page, email: string) {
  return page.getByTestId(`member-${email}`)
}
