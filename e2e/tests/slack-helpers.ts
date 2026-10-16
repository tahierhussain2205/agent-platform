import { expect, type Page } from '@playwright/test'

export const FAKES = process.env.FAKES_URL ?? 'http://localhost:4100'

/** Open the Slack stand-in's UI as a given person. */
export async function openSlack(page: Page, team: string, userLabel: RegExp | string) {
  await page.goto(`${FAKES}/slack/ui`)
  await page.getByLabel('Workspace').selectOption(team)
  await expect(page.getByLabel('Signed in as')).toBeVisible()
  const option = page.getByLabel('Signed in as').locator('option', { hasText: userLabel })
  await page.getByLabel('Signed in as').selectOption((await option.getAttribute('value'))!)
}

/** The signed in person's DM with the agent. A click during a re-render (right after switching
 * person) can be lost, so it retries until the DM is open. */
export async function openDm(page: Page) {
  await expect(async () => {
    await page.getByTestId('dm-agent').click()
    await expect(page.locator('#title')).toHaveText('Agent (direct message)', { timeout: 1_000 })
  }).toPass({ timeout: 10_000 })
}

/** Open a channel; retried like openDm (the UI re-renders every second and after switching person). */
export async function openChannel(page: Page, name: string) {
  await expect(async () => {
    await page
      .getByRole('navigation', { name: 'Channels' })
      .getByRole('button', { name: new RegExp(`^(#|🔒) ${name}`) })
      .click()
    await expect(page.locator('#title')).toContainText(name, { timeout: 1_000 })
  }).toPass({ timeout: 10_000 })
}

/** Add or remove someone (the agent: U0ACMEBOT) in a channel of the Slack stand-in. */
export async function join(team: string, channel: string, user: string, joined = true) {
  await fetch(`${FAKES}/slack/_ui/join`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ team, channel, user, join: joined }),
  })
}

/** Post a message and return its timestamp (data-ts of the new message). Other specs may post in
 * the same channel at the same time, so it looks for a new message with this text, not a count. */
export async function post(page: Page, text: string): Promise<string> {
  const messages = page.getByTestId('message')
  const tsOf = (needle?: string) =>
    messages.evaluateAll(
      (els, n) =>
        els
          .filter((e) => n === undefined || (e.textContent ?? '').includes(n))
          .map((e) => e.getAttribute('data-ts') ?? ''),
      needle,
    )
  const before = new Set(await tsOf())
  await page.locator('#composer').fill(text)
  await page.locator('#send').click()
  const needle = text.replace(/@agent\s*/i, '') // a mention renders as the bot's name
  let ts = ''
  await expect(async () => {
    ts = (await tsOf(needle)).find((t) => t && !before.has(t)) ?? ''
    expect(ts).not.toBe('')
  }).toPass({ timeout: 10_000 })
  return ts
}

export async function openThread(page: Page, ts: string) {
  await page
    .locator(`[data-testid="message"][data-ts="${ts}"]`)
    .getByRole('button', { name: /repl/ })
    .first()
    .click()
  await expect(page.getByTestId('thread')).toBeVisible()
}

export async function newTeam(
  name: string,
  people: { id: string; email: string; name: string }[] = [],
) {
  const response = await fetch(`${FAKES}/_admin/slack/teams`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      name,
      people,
      channels: [
        {
          id: `C${Date.now().toString(36).toUpperCase()}`,
          name: 'general',
          members: people.map((p) => p.id),
        },
      ],
    }),
  })
  return ((await response.json()) as { id: string }).id
}
