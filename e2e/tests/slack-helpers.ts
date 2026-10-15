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

export async function openChannel(page: Page, name: string) {
  await page
    .getByRole('navigation', { name: 'Channels' })
    .getByRole('button', { name: new RegExp(`^(#|🔒) ${name}`) })
    .click()
  await expect(page.locator('#title')).toContainText(name)
}

/** Post a message and return its timestamp (data-ts of the new message). */
export async function post(page: Page, text: string): Promise<string> {
  const before = await page.getByTestId('message').count()
  await page.locator('#composer').fill(text)
  await page.locator('#send').click()
  await expect(page.getByTestId('message')).toHaveCount(before + 1)
  return (await page.getByTestId('message').last().getAttribute('data-ts'))!
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
