import { expect, test, type Browser, type Page } from '@playwright/test'
import { memberRow, signIn, signUp, trackErrors, uid } from './helpers'
import { FAKES, join, openChannel, openDm, openSlack, post } from './slack-helpers'

// One story: schedules are created in the console and from Slack, run on time, are controlled from
// App Home, and pause (with a notice to admins) when their owner leaves or their channel goes.
test.describe.configure({ mode: 'serial' })

const AGENT = { timeout: 90_000 } // an agent reply; generous for loaded machines and CI runners

async function schedules(page: Page, email: string) {
  await signIn(page, email)
  await page
    .getByRole('navigation', { name: 'Main' })
    .getByRole('link', { name: 'Schedules' })
    .click()
  await expect(page.getByRole('heading', { level: 1, name: 'Scheduled reports' })).toBeVisible()
}

async function consolePage(browser: Browser, email: string) {
  const page = await (await browser.newContext()).newPage()
  await schedules(page, email)
  return page
}

/** A daily time a little ahead (UTC): the schedule's next run, soon. */
function soon(): { time: string; cron: string } {
  const at = new Date(Date.now() + (new Date().getUTCSeconds() > 40 ? 120_000 : 60_000))
  const hh = String(at.getUTCHours()).padStart(2, '0')
  const mm = String(at.getUTCMinutes()).padStart(2, '0')
  return { time: `${hh}:${mm}`, cron: `${at.getUTCMinutes()} ${at.getUTCHours()} * * *` }
}

async function newSchedule(
  page: Page,
  name: string,
  opts: { channels: string[]; time: string; deliverTo?: string },
) {
  await page.getByRole('button', { name: 'New schedule' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Name').fill(name)
  for (const c of opts.channels) await dialog.getByLabel(`Summarize #${c}`).check()
  await dialog.getByLabel('Cadence').selectOption('daily')
  await dialog.getByLabel('Time', { exact: true }).fill(opts.time)
  await dialog.getByLabel('Time zone').selectOption('UTC')
  if (opts.deliverTo) {
    await dialog.getByLabel('Post in a channel').check()
    await dialog.getByLabel('Delivery channel').selectOption({ label: `#${opts.deliverTo}` })
  }
  await expect(dialog.getByTestId('cadence-preview')).toContainText(`Every day at ${opts.time}`)
  await dialog.getByRole('button', { name: 'Create schedule' }).click()
  await expect(dialog).toBeHidden()
  return page.getByRole('listitem', { name })
}

async function dms(page: Page, who: RegExp | string) {
  await openSlack(page, 'T0ACME', who)
  await openDm(page)
  return page.getByTestId('message')
}

test('a report scheduled in the console runs on time and arrives by DM', async ({
  browser,
  page,
}) => {
  test.setTimeout(240_000) // waits for the schedule's minute
  const errors = trackErrors(page)
  const t = uid()
  const bob = await consolePage(browser, 'bob@acme.test')
  const { time } = soon()
  const row = await newSchedule(bob, `Bugs digest ${t}`, { channels: ['bugs'], time })
  await expect(row).toContainText('Summary of #bugs')
  await expect(row).toContainText(`Every day at ${time} (UTC)`)
  await expect(row).toContainText('by direct message')

  const messages = await dms(page, 'Bob Builder')
  const report = messages.filter({ hasText: `Bugs digest ${t}` })
  await expect(report).toBeVisible({ timeout: 150_000 }) // its minute, plus a few seconds of jitter
  await expect(report).toContainText('Messages:')
  await expect(report).toContainText('Scheduled report')
  await expect(async () => {
    await bob.reload()
    await expect(bob.getByRole('listitem', { name: `Bugs digest ${t}` })).toContainText(
      'Last run:',
      { timeout: 1_000 },
    )
  }).toPass({ timeout: 45_000 })
  expect(errors).toEqual([])
})

test('a schedule asked for in Slack is confirmed, then paused from App Home', async ({
  browser,
  page,
}) => {
  await openSlack(page, 'T0ACME', 'Bob Builder')
  await openChannel(page, 'general')
  const ts = await post(page, '@agent every weekday at 9am send me a summary of #bugs')
  const message = page.locator(`[data-testid="message"][data-ts="${ts}"]`)
  await message.getByRole('button', { name: /\d+ repl/ }).click({ timeout: AGENT.timeout })
  const thread = page.getByTestId('thread')
  await expect(thread).toContainText('Weekday #bugs summary', AGENT)
  await expect(thread).toContainText('Every weekday at 09:00')
  await thread.getByRole('button', { name: 'Create schedule' }).click()
  await expect(thread).toContainText('✅ Scheduled by', { timeout: 30_000 })

  const bob = await consolePage(browser, 'bob@acme.test')
  const row = bob.getByRole('listitem', { name: 'Weekday #bugs summary' })
  await expect(row).toContainText('Active')
  const id = (await row.getAttribute('data-testid'))!.replace('schedule-', '')

  await page.getByTestId('home-tab').click()
  const home = page.locator('#messages')
  await expect(home).toContainText('Your scheduled reports', { timeout: 30_000 }) // published by the worker
  await expect(home).toContainText('Weekday #bugs summary')
  await home.locator(`button[data-block="schedule:${id}"]`, { hasText: 'Pause' }).click()
  await expect(async () => {
    await bob.reload()
    await expect(bob.getByRole('listitem', { name: 'Weekday #bugs summary' })).toContainText(
      'paused by bob@acme.test in Slack',
      { timeout: 1_000 },
    )
  }).toPass({ timeout: 45_000 })
})

test('removing the owner pauses their schedule and tells the admins', async ({ browser, page }) => {
  const t = uid()
  const email = `owner-${t}@acme.test`
  // Alice invites a new member, who signs up and accepts.
  const alice = await (await browser.newContext()).newPage()
  await signIn(alice, 'alice@acme.test')
  await alice.getByRole('link', { name: 'Members' }).first().click()
  await alice.getByLabel('Email').fill(email)
  await alice.getByRole('button', { name: 'Send invitation' }).click()
  await expect(alice.getByText(`Invitation sent to ${email}.`)).toBeVisible()
  const owner = await (await browser.newContext()).newPage()
  await signUp(owner, email, 'Olive', 'Owner')
  await owner.getByRole('button', { name: 'Accept' }).first().click()
  await expect(owner.getByTestId('current-user')).toHaveText(email, { timeout: 20_000 })

  // They are in the Slack workspace and say hello, so the agent knows their Slack account.
  const added = await fetch(`${FAKES}/_admin/slack/users`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ team: 'T0ACME', email, name: `Olive ${t}`, channels: ['C0GENERAL'] }),
  })
  expect(added.ok).toBe(true)
  await openSlack(page, 'T0ACME', `Olive ${t}`)
  await openChannel(page, 'general')
  const hello = await post(page, `@agent what was discussed today? ${t}`)
  await expect(
    page
      .locator(`[data-testid="message"][data-ts="${hello}"]`)
      .getByRole('button', { name: /repl/ }),
  ).toBeVisible(AGENT)

  // They schedule a report, then Alice removes them.
  await owner
    .getByRole('navigation', { name: 'Main' })
    .getByRole('link', { name: 'Schedules' })
    .click()
  await expect(owner.getByRole('button', { name: 'New schedule' })).toBeEnabled({ timeout: 15_000 })
  await newSchedule(owner, `Olive digest ${t}`, { channels: ['general'], time: '08:00' })
  await alice.reload()
  const row = memberRow(alice, email)
  await row.getByRole('button', { name: 'Remove' }).click()
  await alice.getByRole('dialog').getByRole('button', { name: 'Remove member' }).click()
  await expect(memberRow(alice, email)).toHaveCount(0)

  await alice
    .getByRole('navigation', { name: 'Main' })
    .getByRole('link', { name: 'Schedules' })
    .click()
  const schedule = alice.getByRole('listitem', { name: `Olive digest ${t}` })
  await expect(schedule).toContainText('Owner removed')
  await expect(schedule.getByRole('button', { name: 'Reassign' })).toBeVisible()

  const messages = await dms(page, 'Alice Admin')
  const notice = messages.filter({ hasText: `Olive digest ${t}` })
  await expect(notice).toContainText(`owned by ${email} was paused`, AGENT)
  await expect(notice).toContainText('Reassign it or delete it')
})

test('a delivery channel taken off the allowlist pauses its schedules', async ({
  browser,
  page,
}) => {
  const t = uid()
  const name = `reports-${t}`
  // A new channel with the agent and Alice in it.
  const created = await fetch(`${FAKES}/_admin/slack/channels`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ team: 'T0ACME', name, members: [] }),
  })
  const { id } = (await created.json()) as { id: string }
  for (const user of ['U0ACMEBOT', 'U0ALICE']) await join('T0ACME', id, user)
  const alice = await (await browser.newContext()).newPage()
  await signIn(alice, 'alice@acme.test')
  await alice.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Slack' }).click()
  const allow = alice.getByLabel(`Allow the agent in ${name}`)
  await expect(allow).toBeVisible({ timeout: 15_000 })
  await allow.check()
  await expect(allow).toBeEnabled() // saved (it shows the requested state while saving)
  await expect(allow).toBeChecked()
  // Member lists are kept for allowed channels only: allowing it starts a sync of its members.
  await expect(async () => {
    await alice.reload()
    // The members column: Alice (the agent itself is not counted).
    await expect(alice.getByTestId(`channel-${name}`).locator('td').nth(2)).toHaveText('1', {
      timeout: 1_000,
    })
  }).toPass({ timeout: 45_000 })

  await alice
    .getByRole('navigation', { name: 'Main' })
    .getByRole('link', { name: 'Schedules' })
    .click()
  const row = await newSchedule(alice, `Channel digest ${t}`, {
    channels: ['bugs'],
    time: '07:30',
    deliverTo: name,
  })
  await expect(row).toContainText(`in #${name}`)

  await alice.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Slack' }).click()
  await alice.getByLabel(`Allow the agent in ${name}`).uncheck()
  await expect(alice.getByText('1 schedule(s) delivering here were paused.')).toBeVisible()
  await alice
    .getByRole('navigation', { name: 'Main' })
    .getByRole('link', { name: 'Schedules' })
    .click()
  await expect(alice.getByRole('listitem', { name: `Channel digest ${t}` })).toContainText(
    'Access lost',
  )

  const notice = (await dms(page, 'Alice Admin')).filter({ hasText: `Channel digest ${t}` })
  await expect(notice).toContainText('was paused', AGENT)
})
