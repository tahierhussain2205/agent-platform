import { execSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { chromium } from '@playwright/test'

async function until(what: string, check: () => Promise<boolean>, seconds: number) {
  const deadline = Date.now() + seconds * 1000
  while (Date.now() < deadline) {
    if (await check().catch(() => false)) return
    await new Promise((resolve) => setTimeout(resolve, 1000))
  }
  throw new Error(`${what} not ready after ${seconds}s`)
}

/** Back to the seed (Clerk stand-in data with its keys kept, the seeded database rows), then a warm
 * stack: right after `make dev` the API may still be connecting and Vite compiles the console on
 * its first load, which on a busy machine outlasts a test's waits. */
export default async function globalSetup() {
  const fakes = process.env.FAKES_URL ?? 'http://localhost:4100'
  const api = process.env.API_URL ?? 'http://localhost:8000'
  const web = process.env.WEB_URL ?? 'http://localhost:5173'
  const response = await fetch(`${fakes}/_admin/reset`, { method: 'POST' })
  if (!response.ok) throw new Error(`fakes reset failed: ${response.status}`)
  const root = fileURLToPath(new URL('..', import.meta.url))
  execSync('docker compose exec -T api app seed', { cwd: root, stdio: 'inherit' })

  await until('the API', async () => (await fetch(`${api}/readyz`)).ok, 90)
  const browser = await chromium.launch()
  try {
    const page = await browser.newPage()
    await page.goto(`${web}/sign-in`, { timeout: 120_000 })
    await page.getByRole('heading', { name: 'Sign in' }).waitFor({ timeout: 120_000 })
  } finally {
    await browser.close()
  }
}
