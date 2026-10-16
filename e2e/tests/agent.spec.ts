import { expect, test, type Page } from '@playwright/test'
import { signIn, uid } from './helpers'
import { openChannel, openDm, openSlack, post } from './slack-helpers'

const AGENT = { timeout: 45_000 }

/** Post as someone in a channel, as they would in Slack. */
async function say(page: Page, team: string, who: string, channel: string, text: string) {
  await openSlack(page, team, who)
  await openChannel(page, channel)
  return post(page, text)
}

/** Open the thread of a message once the agent has replied in it. */
async function threadOf(page: Page, ts: string, replies = /\d+ repl/) {
  const message = page.locator(`[data-testid="message"][data-ts="${ts}"]`)
  await expect(message.getByRole('button', { name: replies })).toBeVisible(AGENT)
  await message.getByRole('button', { name: replies }).click()
  const thread = page.getByTestId('thread')
  await expect(thread).toBeVisible()
  return thread
}

test('summarize today’s discussion in #bugs: a sourced answer in the thread', async ({ page }) => {
  const t = uid()
  await say(page, 'T0ACME', 'Quinn Tester', 'bugs', `Checkout fails for carts over 20 items ${t}`)
  await say(page, 'T0ACME', 'Alice Admin', 'bugs', `Rolled back cart-service to v41 ${t}`)
  const ts = await say(
    page,
    'T0ACME',
    'Bob Builder',
    'general',
    "@agent summarize today's discussion in #bugs",
  )
  const thread = await threadOf(page, ts)
  await expect(thread).toContainText('✅ Done', AGENT)
  await expect(thread).toContainText(`Checkout fails for carts over 20 items ${t}`)
  await expect(thread).toContainText(`Rolled back cart-service to v41 ${t}`)
  await expect(thread).toContainText('Sources')
  await expect(thread.getByRole('link', { name: /^#bugs, / }).first()).toHaveAttribute(
    'href',
    /\/slack\/ui#T0ACME\/C0BUGS\//,
  )
})

test('someone who is not a member of a private channel is refused', async ({ page }) => {
  const ts = await say(page, 'T0ACME', 'Bob Builder', 'general', '@agent summarize #leads-private')
  const thread = await threadOf(page, ts)
  await expect(thread).toContainText('✅ Done', AGENT)
  await expect(thread).toContainText('not a member of #leads-private')
  await expect(thread).not.toContainText('reorg')
})

test('an answer drawn from a private channel is sent privately', async ({ page }) => {
  const t = uid()
  await say(page, 'T0ACME', 'Alice Admin', 'leads-private', `Offsite moved to May 12 ${t}`)
  const ts = await say(
    page,
    'T0ACME',
    'Alice Admin',
    'general',
    "@agent summarize today's discussion in #leads-private",
  )
  const thread = await threadOf(page, ts)
  await expect(thread).toContainText('I sent you the answer privately', AGENT)
  await expect(thread).not.toContainText(`Offsite moved to May 12 ${t}`)
  await openDm(page)
  await expect(
    page.getByTestId('message').filter({ hasText: `Offsite moved to May 12 ${t}` }),
  ).toBeVisible()
})

test('sharing in another channel waits for approval, then posts', async ({ page }) => {
  const t = uid()
  await say(page, 'T0ACME', 'Quinn Tester', 'bugs', `Search is slow in EU ${t}`)
  const ts = await say(
    page,
    'T0ACME',
    'Bob Builder',
    'general',
    "@agent summarize today's discussion in #bugs and share it in #general",
  )
  const thread = await threadOf(page, ts)
  const approval = thread.getByTestId('message').filter({ hasText: 'Approval needed' })
  await expect(approval).toBeVisible(AGENT)
  await expect(approval).toContainText('irreversible')
  await expect(thread).toContainText('Waiting for approval')
  await approval.getByRole('button', { name: 'Approve' }).click()
  await expect(thread).toContainText('Approved by @Bob Builder', AGENT)
  await expect(thread).toContainText('✅ Done', AGENT)
  // The summary is now a top level message in #general.
  await expect(
    page
      .locator('#messages [data-testid="message"]')
      .filter({ hasText: `Search is slow in EU ${t}` })
      .filter({ hasNotText: 'summarize' }),
  ).toHaveCount(1, AGENT)
})

test('a rejected share posts nothing, and only the requester or an admin decides', async ({
  page,
}) => {
  const t = uid()
  await say(page, 'T0ACME', 'Quinn Tester', 'bugs', `Rejected share ${t}`)
  const ts = await say(
    page,
    'T0ACME',
    'Bob Builder',
    'general',
    "@agent summarize today's discussion in #bugs and share it in #general",
  )
  await threadOf(page, ts)
  // Quinn is neither the requester nor an admin.
  await openSlack(page, 'T0ACME', 'Quinn Tester')
  await openChannel(page, 'general')
  const asQuinn = await threadOf(page, ts)
  const approval = asQuinn.getByTestId('message').filter({ hasText: 'Approval needed' })
  await expect(approval).toBeVisible(AGENT)
  await approval.getByRole('button', { name: 'Reject' }).click()
  await expect(page.getByTestId('ephemeral').last()).toContainText(
    'Only the person who asked or an admin can decide this.',
    AGENT,
  )
  // Alice is an admin.
  await openSlack(page, 'T0ACME', 'Alice Admin')
  await openChannel(page, 'general')
  const asAlice = await threadOf(page, ts)
  await asAlice
    .getByTestId('message')
    .filter({ hasText: 'Approval needed' })
    .getByRole('button', { name: 'Reject' })
    .click()
  await expect(asAlice).toContainText('Rejected by @Alice Admin', AGENT)
  await expect(asAlice).toContainText('not done (rejected)', AGENT)
  await expect(
    page.locator('#messages [data-testid="message"]').filter({ hasText: `Rejected share ${t}` }),
  ).toHaveCount(0)
})

test('the requester can cancel a running task', async ({ page }) => {
  const ts = await say(
    page,
    'T0ACME',
    'Bob Builder',
    'general',
    '@agent summarize #bugs and share it in #general',
  )
  const thread = await threadOf(page, ts)
  await expect(thread).toContainText('Waiting for approval', AGENT)
  await thread.getByRole('button', { name: 'Cancel' }).click()
  await expect(thread).toContainText('🛑 Cancelled.', AGENT)
  await expect(thread.getByRole('button', { name: 'Cancel' })).toHaveCount(0)
})

test('a spent daily token limit stops the agent before it starts', async ({ page, browser }) => {
  // Globex, so no other test is affected by the limit.
  const admin = await (await browser.newContext()).newPage()
  await signIn(admin, 'dave@globex.test')
  await admin.getByRole('link', { name: 'Settings' }).click()
  await admin.getByLabel('Tokens per user per day').fill('1')
  await admin.getByRole('button', { name: 'Save settings' }).click()
  await expect(admin.getByText('Settings saved.')).toBeVisible()
  try {
    const first = await say(page, 'T0GLOBEX', 'Erin', 'general', '@agent summarize #general')
    let thread = await threadOf(page, first)
    // The first request may still fit; spend what is left, then the limit applies.
    await expect(thread).toContainText(/✅ Done|I can't start|I stopped because/, AGENT)
    const second = await say(page, 'T0GLOBEX', 'Erin', 'general', '@agent and again?')
    thread = await threadOf(page, second)
    await expect(thread).toContainText("I can't start: your daily token limit", AGENT)
  } finally {
    await admin.getByLabel('Tokens per user per day').fill('')
    await admin.getByRole('button', { name: 'Save settings' }).click()
    await expect(admin.getByText('Settings saved.')).toBeVisible()
    await admin.context().close()
  }
})

test('agent decisions appear in the console audit log', async ({ page, browser }) => {
  const ts = await say(page, 'T0ACME', 'Bob Builder', 'general', '@agent summarize #leads-private')
  await expect((await threadOf(page, ts)).getByText('✅ Done')).toBeVisible(AGENT)
  const console_ = await (await browser.newContext()).newPage()
  await signIn(console_, 'alice@acme.test')
  await console_.getByRole('link', { name: 'Audit log' }).click()
  await console_.getByLabel('Filter by actor').selectOption('agent')
  const denied = console_
    .getByTestId('audit-row')
    .filter({ hasText: 'slack.read_channel #leads-private' })
    .first()
  await expect(denied).toContainText('Agent for bob@acme.test')
  await expect(denied).toContainText('denied')
  await expect(denied).toContainText('not a member of #leads-private')
  await console_.context().close()
})
