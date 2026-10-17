import { expect, test, type Page } from '@playwright/test'
import { signIn } from './helpers'

// Every console page an admin sees.
const PAGES = [
  '/',
  '/tasks',
  '/members',
  '/roles',
  '/slack',
  '/integrations',
  '/catalog',
  '/repositories',
  '/schedules',
  '/usage',
  '/settings',
  '/audit',
]

async function open(page: Page, path: string) {
  await page.goto(path)
  await expect(page.getByTestId('current-user')).toBeAttached() // hidden on phones
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  await expect(page.locator('main [role=status][aria-label=Loading]')).toHaveCount(0)
}

/** Visible clickable elements without a pointer (or, when disabled, without "not allowed"). */
function cursorProblems(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const clickable =
      'a[href],button,[role=button],[role=tab],select,summary,input[type=checkbox],input[type=radio],' +
      'input[type=file],label:has(input[type=checkbox]),label:has(input[type=radio])'
    const problems: string[] = []
    for (const el of document.querySelectorAll<HTMLElement>(clickable)) {
      const box = el.getBoundingClientRect()
      if (!box.width || !box.height) continue
      const disabled =
        el.matches(':disabled,[aria-disabled=true]') ||
        (el.tagName === 'LABEL' && !!el.querySelector('input:disabled'))
      const want = disabled ? 'not-allowed' : 'pointer'
      const cursor = getComputedStyle(el).cursor
      if (cursor !== want) {
        const name = (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 40)
        problems.push(`${el.tagName.toLowerCase()} "${name}": cursor ${cursor}, expected ${want}`)
      }
    }
    return problems
  })
}

/** Elements painted in a color the theme should never use (pure white on dark, pure black on light). */
function offThemeBackgrounds(page: Page, color: string): Promise<string[]> {
  return page.evaluate((forbidden) => {
    const found: string[] = []
    for (const el of document.querySelectorAll<HTMLElement>('body *')) {
      const box = el.getBoundingClientRect()
      if (!box.width || !box.height) continue
      if (getComputedStyle(el).backgroundColor === forbidden) {
        found.push(`${el.tagName.toLowerCase()}.${String(el.className).slice(0, 60)}`)
      }
    }
    return found
  }, color)
}

const background = (page: Page, selector: string) =>
  page.locator(selector).first().evaluate((el) => getComputedStyle(el).backgroundColor)

test.describe('themes', () => {
  test('dark and light switch, stay after a reload and color the whole console', async ({ page }) => {
    await signIn(page, 'alice@acme.test')
    const html = page.locator('html')

    await page.getByRole('button', { name: 'Dark theme' }).click()
    await expect(html).toHaveClass(/\bdark\b/)
    await expect(page.getByRole('button', { name: 'Dark theme' })).toHaveAttribute('aria-pressed', 'true')
    expect(await background(page, 'body')).toBe('rgb(0, 0, 0)')
    expect(await background(page, 'header')).toBe('rgb(18, 18, 18)')

    await page.reload()
    await expect(page.getByTestId('current-user')).toBeVisible()
    await expect(html).toHaveClass(/\bdark\b/)

    await page.getByRole('button', { name: 'Light theme' }).click()
    await expect(html).not.toHaveClass(/\bdark\b/)
    expect(await background(page, 'body')).toBe('rgb(245, 245, 245)')
    expect(await background(page, 'header')).toBe('rgb(255, 255, 255)')

    await page.getByRole('button', { name: 'Use the system theme' }).click()
  })

  test('the system theme is followed until someone chooses', async ({ browser }) => {
    const context = await browser.newContext({ colorScheme: 'dark' })
    const page = await context.newPage()
    const html = page.locator('html')
    await page.goto('/sign-in')
    await expect(html).toHaveClass(/\bdark\b/) // before any choice: the system's
    await page.emulateMedia({ colorScheme: 'light' })
    await expect(html).not.toHaveClass(/\bdark\b/) // follows a change of the system's
    await page.getByRole('button', { name: 'Dark theme' }).click()
    await page.emulateMedia({ colorScheme: 'light' })
    await page.reload()
    await expect(html).toHaveClass(/\bdark\b/) // a choice wins over the system
    await context.close()
  })

  test('no page keeps colors from the other theme', async ({ page }) => {
    await signIn(page, 'alice@acme.test')
    for (const [theme, forbidden] of [
      ['Dark theme', 'rgb(255, 255, 255)'],
      ['Light theme', 'rgb(0, 0, 0)'],
    ] as const) {
      await page.getByRole('button', { name: theme }).click()
      for (const path of PAGES) {
        await open(page, path)
        expect(await offThemeBackgrounds(page, forbidden), `${theme} ${path}`).toEqual([])
      }
    }
    await page.getByRole('button', { name: 'Use the system theme' }).click()
  })

  test('sign in follows the theme too', async ({ browser }) => {
    const context = await browser.newContext({ colorScheme: 'dark' })
    const page = await context.newPage()
    await page.goto('/sign-in')
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
    expect(await offThemeBackgrounds(page, 'rgb(255, 255, 255)')).toEqual([])
    await context.close()
  })
})

test('everything clickable shows a pointer', async ({ page }) => {
  await page.goto('/sign-in')
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
  expect(await cursorProblems(page), '/sign-in').toEqual([])

  await signIn(page, 'alice@acme.test')
  for (const path of PAGES) {
    await open(page, path)
    expect(await cursorProblems(page), path).toEqual([])
  }

  await open(page, '/schedules')
  await page.getByRole('button', { name: 'New schedule' }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  expect(await cursorProblems(page), 'new schedule dialog').toEqual([])
})

test('pages fit a phone screen without scrolling sideways', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 })
  await signIn(page, 'alice@acme.test')
  for (const path of PAGES) {
    await open(page, path)
    const [scroll, width] = await page.evaluate(() => [
      document.documentElement.scrollWidth,
      document.documentElement.clientWidth,
    ])
    expect(scroll, path).toBeLessThanOrEqual(width)
  }
})
