import { expect, test } from '@playwright/test'
import { founder, memberRow, signIn, uid } from './helpers'
import { join, newTeam, openChannel, openDm, openSlack, openThread, post } from './slack-helpers'

test('an admin connects a new Slack workspace from the console', async ({ browser }) => {
  const admin = await founder(browser, `Slackers ${uid()}`)
  const teamName = `Workspace ${uid()}`
  await newTeam(teamName, [
    { id: `U${uid().toUpperCase()}`, email: `someone-${uid()}@ws.test`, name: 'Someone' },
  ])
  await admin.page.getByRole('link', { name: 'Slack' }).click()
  await expect(admin.page.getByText('Slack is not connected yet.')).toBeVisible()
  await admin.page.getByRole('button', { name: 'Connect Slack' }).click()
  // The Slack stand-in's OAuth page: pick the workspace and allow.
  // Console, API and stand-in redirects: slow on a loaded machine right after the stack starts.
  await expect(admin.page.getByRole('heading', { name: /requesting permission/ })).toBeVisible({
    timeout: 30_000,
  })
  await admin.page.getByLabel(new RegExp(teamName)).check()
  await expect(admin.page.getByText('users:read.email')).toBeVisible()
  await admin.page.getByRole('button', { name: 'Allow' }).click()
  // Back in the console.
  await expect(
    admin.page.getByText(`Connected to the ${teamName} Slack workspace`, { exact: false }),
  ).toBeVisible()
  await expect(admin.page.getByText(`Connected to ${teamName}`)).toBeVisible()
  // The first sync runs in the worker; the workspace's channel appears.
  await expect(async () => {
    await admin.page.reload()
    await expect(admin.page.getByTestId('channel-general')).toBeVisible({ timeout: 1_000 })
  }).toPass({ timeout: 60_000 })
  await expect(admin.page.getByTestId('channel-general')).toContainText('Agent not in channel')

  // Disconnect.
  await admin.page.getByRole('button', { name: 'Disconnect' }).click()
  await admin.page.getByRole('dialog').getByRole('button', { name: 'Disconnect' }).click()
  await expect(admin.page.getByText('Slack is not connected yet.')).toBeVisible()
  await admin.context.close()
})

test('a member mentions the agent in an allowlisted channel and gets a reply in the thread', async ({
  page,
}) => {
  await openSlack(page, 'T0ACME', 'Bob Builder')
  await openChannel(page, 'bugs')
  const ts = await post(page, `@agent what broke today? ${uid()}`)
  const message = page.locator(`[data-testid="message"][data-ts="${ts}"]`)
  // The agent keeps one status message and posts its answer: two replies.
  await expect(message.getByRole('button', { name: '2 replies' })).toBeVisible({ timeout: 90_000 })
  await openThread(page, ts)
  await expect(page.getByTestId('thread')).toContainText('✅ Done', { timeout: 30_000 }) // after the answer
})

test('a channel that is not allowed gets a private explanation', async ({ page, browser }) => {
  // Alice adds the agent to #random in Slack; the console sees it, but #random is not allowed yet.
  await join('T0ACME', 'C0RANDOM', 'U0ACMEBOT', false) // whatever an earlier run left behind
  await openSlack(page, 'T0ACME', 'Alice Admin')
  await openChannel(page, 'random')
  await page.getByRole('button', { name: 'Add Agent to this channel' }).click()
  await expect(page.getByRole('button', { name: 'Add Agent to this channel' })).toHaveCount(0)
  const console_ = await (await browser.newContext()).newPage()
  await signIn(console_, 'alice@acme.test')
  await console_.getByRole('link', { name: 'Slack' }).click()
  await expect(async () => {
    await console_.reload()
    await expect(console_.getByTestId('channel-random')).toContainText('Agent is in the channel', {
      timeout: 1_000,
    })
  }).toPass({ timeout: 60_000 })
  await expect(console_.getByLabel('Allow the agent in random')).not.toBeChecked()

  await openSlack(page, 'T0ACME', 'Bob Builder')
  await openChannel(page, 'random')
  await post(page, `@agent can you help here? ${uid()}`)
  await expect(page.getByTestId('ephemeral').last()).toContainText(
    "I'm not enabled in this channel",
    { timeout: 60_000 },
  )

  // The admin allows #random; now the agent answers there.
  await console_.getByLabel('Allow the agent in random').check()
  await expect(console_.getByLabel('Allow the agent in random')).toBeEnabled() // saved
  await expect(console_.getByLabel('Allow the agent in random')).toBeChecked()
  // Allowing it starts a sync of its members (kept for allowed channels only): wait for Bob's.
  await expect(async () => {
    await console_.reload()
    await expect(console_.getByTestId('channel-random').locator('td').nth(2)).toHaveText('4', {
      timeout: 1_000,
    })
  }).toPass({ timeout: 30_000 })
  const ts = await post(page, `@agent and now? ${uid()}`)
  await expect(
    page
      .locator(`[data-testid="message"][data-ts="${ts}"]`)
      .getByRole('button', { name: '2 replies' }),
  ).toBeVisible({ timeout: 90_000 })
  await console_.getByLabel('Allow the agent in random').uncheck()
  await console_.context().close()
})

test('an uninvited person gets the access request flow and an admin invites them from Slack', async ({
  page,
  browser,
}) => {
  test.setTimeout(240_000) // three round trips through the agent and the console
  await openSlack(page, 'T0ACME', 'Mallory Outsider')
  await openChannel(page, 'general')
  await post(page, `@agent hello? ${uid()}`)
  await expect(page.getByTestId('ephemeral').last()).toContainText(
    "You don't have access to the agent in Acme Corp yet",
    { timeout: 60_000 },
  )

  // Alice gets a DM with Invite and Dismiss.
  await openSlack(page, 'T0ACME', 'Alice Admin')
  await openDm(page)
  const ask = page.getByTestId('message').filter({ hasText: 'asked to use the agent' }).last()
  await expect(ask).toContainText('mallory@acme.test', { timeout: 60_000 })
  await ask.getByRole('button', { name: 'Invite' }).click()
  await expect(
    page.getByTestId('message').filter({ hasText: 'invited mallory@acme.test' }),
  ).toBeVisible({ timeout: 60_000 })

  // The console shows Mallory as invited (Clerk invitation, mirrored).
  const console_ = await (await browser.newContext()).newPage()
  await signIn(console_, 'alice@acme.test')
  await console_.getByRole('link', { name: 'Members' }).click()
  await expect(memberRow(console_, 'mallory@acme.test').getByText('Invited')).toBeVisible()
  await expect(console_.getByTestId('access-request-mallory@acme.test')).toHaveCount(0)
  await console_.context().close()

  // As an invited user, Mallory can now use the agent.
  await openSlack(page, 'T0ACME', 'Mallory Outsider')
  await openChannel(page, 'general')
  const ts = await post(page, `@agent now? ${uid()}`)
  await expect(
    page
      .locator(`[data-testid="message"][data-ts="${ts}"]`)
      .getByRole('button', { name: '2 replies' }),
  ).toBeVisible({ timeout: 90_000 })
})

test('App Home shows the org and the Ask/Auto choice, and changes stick', async ({ page }) => {
  await openSlack(page, 'T0ACME', 'Bob Builder')
  await page.getByTestId('home-tab').click()
  await expect(page.getByTestId('messages')).toContainText('Acme Corp', { timeout: 60_000 })
  const mode = page.getByLabel('How should I act?')
  await expect(mode).toHaveValue('ask')
  // The stand-in UI pauses its refresh while a select has focus: leave it after choosing.
  await mode.selectOption('auto')
  await mode.blur()
  await expect(page.getByTestId('messages')).toContainText('working in auto mode', {
    timeout: 60_000,
  })
  await mode.selectOption('ask')
  await mode.blur()
  await expect(page.getByTestId('messages')).toContainText('working in ask mode', {
    timeout: 60_000,
  })
})
