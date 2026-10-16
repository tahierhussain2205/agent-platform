import { expect, test, type Page } from '@playwright/test'
import { signIn, trackErrors, uid } from './helpers'
import { openChannel, openSlack, post } from './slack-helpers'

// One story in order: the seed leaves integrations disabled and unconnected, the admin connects,
// a member links, then the agent uses them.
test.describe.configure({ mode: 'serial' })

const AGENT = { timeout: 45_000 }

const card = (page: Page, title: string) => page.getByRole('region', { name: title, exact: true })

async function integrations(page: Page, email: string) {
  await signIn(page, email)
  await page
    .getByRole('navigation', { name: 'Main' })
    .getByRole('link', { name: 'Integrations' })
    .click()
  await expect(page.getByRole('heading', { level: 1, name: 'Integrations' })).toBeVisible()
}

/** Approve on a stand-in consent page as the given account. */
async function consent(page: Page, account: RegExp | string) {
  await page.getByRole('radio', { name: account }).check()
  await page.getByRole('button', { name: 'Allow' }).click()
}

async function say(page: Page, who: string, channel: string, text: string) {
  await openSlack(page, 'T0ACME', who)
  await openChannel(page, channel)
  return post(page, text)
}

async function threadOf(page: Page, ts: string) {
  const message = page.locator(`[data-testid="message"][data-ts="${ts}"]`)
  await expect(message.getByRole('button', { name: /\d+ repl/ })).toBeVisible(AGENT)
  await message.getByRole('button', { name: /\d+ repl/ }).click()
  const thread = page.getByTestId('thread')
  await expect(thread).toBeVisible()
  await expect(thread).toContainText('✅ Done', AGENT)
  return thread
}

test('an admin enables and connects Jira and GitHub through the providers', async ({ page }) => {
  const errors = trackErrors(page)
  await integrations(page, 'alice@acme.test')
  const jira = card(page, 'Jira')
  await expect(jira.getByTestId('jira-connection')).toContainText('not connected')
  await jira.getByLabel('Enable Jira').check()
  await expect(jira.getByRole('button', { name: 'Link my account' })).toBeVisible()

  await jira.getByRole('button', { name: 'Connect Jira' }).click()
  await expect(page).toHaveTitle('Jira wants to access your Atlassian account')
  await consent(page, /Alice Admin/)
  await expect(page.getByText('Jira is connected.')).toBeVisible()
  await expect(jira.getByTestId('jira-connection')).toContainText('connected as cloud-acme')

  const github = card(page, 'GitHub')
  await github.getByLabel('Enable GitHub').check()
  await github.getByRole('button', { name: 'Connect GitHub' }).click()
  await consent(page, 'alice-gh installing on acme')
  await expect(page.getByText('GitHub is connected.')).toBeVisible()
  await expect(github.getByTestId('github-connection')).toContainText('connected as acme')

  await github.getByRole('button', { name: 'Check now' }).click()
  await expect(github.getByText(/\d+ repositories visible\./)).toBeVisible()
  await expect(github.getByText('Healthy')).toBeVisible()
  expect(errors).toEqual([])
})

test('a cancelled consent changes nothing', async ({ page }) => {
  await integrations(page, 'alice@acme.test')
  await card(page, 'Jira').getByRole('button', { name: 'Reconnect' }).click()
  await page.getByRole('button', { name: 'Deny' }).click()
  await expect(page.getByText('The connection was cancelled at the provider.')).toBeVisible()
  await expect(card(page, 'Jira').getByTestId('jira-connection')).toContainText(
    'connected as cloud-acme', // the org connection is to the Jira site
  )
})

test('a member links their own Jira and GitHub accounts', async ({ page }) => {
  await integrations(page, 'bob@acme.test')
  const jira = card(page, 'Jira')
  await expect(jira.getByLabel('Enable Jira')).toBeDisabled()
  await expect(jira.getByRole('button', { name: /Connect|Reconnect|Disconnect/ })).toHaveCount(0)
  await jira.getByRole('button', { name: 'Link my account' }).click()
  await consent(page, /Bob Builder/)
  await expect(page.getByText('Your Jira account is linked.')).toBeVisible()
  await expect(jira.getByTestId('jira-link')).toContainText('linked as acc-bob')

  const github = card(page, 'GitHub')
  await github.getByRole('button', { name: 'Link my account' }).click()
  await consent(page, 'bob-gh (bob@acme.test)')
  await expect(page.getByText('Your GitHub account is linked.')).toBeVisible()
  await expect(github.getByTestId('github-link')).toContainText('linked as bob-gh')
})

test('the agent answers from Jira and refuses a project the requester cannot browse', async ({
  page,
}) => {
  const ts = await say(page, 'Bob Builder', 'bugs', '@agent what is the status of PAY-101?')
  const thread = await threadOf(page, ts)
  await expect(thread).toContainText('PAY-101')
  await expect(thread).toContainText('Checkout returns 500')

  const sec = await say(page, 'Bob Builder', 'bugs', '@agent what is happening with SEC-3?')
  const refused = await threadOf(page, sec)
  await expect(refused).toContainText("can't browse the project of SEC-3")
  await expect(refused).not.toContainText('staging token')
})

test('a private repository answer goes only to the requester', async ({ page }) => {
  const ts = await say(
    page,
    'Bob Builder',
    'bugs',
    '@agent who owns src/pricing/cart.py in acme/cart-service?',
  )
  const thread = await threadOf(page, ts)
  // Not everyone in #bugs has linked a GitHub account that can read acme/cart-service.
  await expect(thread).toContainText('I sent you the answer privately')
  await expect(thread).not.toContainText('@bob-gh')
  await page.getByTestId('dm-agent').click()
  await expect(page.getByTestId('message').filter({ hasText: '@bob-gh' }).last()).toBeVisible()
})

test('a credential mode override makes each person use their own account', async ({
  browser,
  page,
}) => {
  const admin = await (await browser.newContext()).newPage()
  await integrations(admin, 'alice@acme.test')
  const jira = card(admin, 'Jira')
  await jira.getByText(/^Operations \(/).click()
  await jira.getByLabel('Credential for get_issue').selectOption('user_required')
  await expect(jira.getByText(/1 overridden/)).toBeVisible()

  // Alice has no Jira link of her own; Bob has.
  const alice = await say(page, 'Alice Admin', 'bugs', '@agent what is the status of PAY-102?')
  await expect(await threadOf(page, alice)).toContainText('Link your Jira account')
  const bob = await say(page, 'Bob Builder', 'bugs', '@agent what is the status of PAY-102?')
  await expect(await threadOf(page, bob)).toContainText('Refunds for EU cards')

  await jira.getByRole('button', { name: 'Reset' }).click()
  await expect(jira.getByText(/overridden/)).toHaveCount(0)
  await expect(jira.getByLabel('Credential for get_issue')).toHaveValue('org_with_check')
})

test('a custom MCP server needs approval before it can be enabled', async ({ page }) => {
  const name = `Status ${uid()}`
  const key = `mcp_${name.toLowerCase().replace(' ', '_')}`
  await integrations(page, 'alice@acme.test')
  await page.getByRole('button', { name: 'Add MCP server' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Name').fill(name)
  await dialog.getByLabel('URL').fill('http://fakes:4100/mcp/status')
  await dialog.getByLabel('API key').fill('mcp-key-local')
  await dialog.getByRole('button', { name: 'Add server' }).click()

  const server = card(page, name)
  await expect(server.getByText('Waiting for approval')).toBeVisible()
  await expect(server.getByLabel(`Enable ${name}`)).toHaveCount(0)
  await expect(server.getByTestId(`${key}-connection`)).toContainText('API key stored')

  await server.getByRole('button', { name: 'Approve' }).click()
  await expect(server.getByText('2 tools available.')).toBeVisible()
  await expect(server.getByText('Healthy')).toBeVisible()
  await expect(server.getByText('Waiting for approval')).toHaveCount(0)
  await server.getByLabel(`Enable ${name}`).check()
  await server.getByText(/^Operations \(2/).click()
  // Tool annotations decide the tier: a destructive tool is irreversible (always approved).
  await expect(server.getByTestId(`op-${key}-restart_service`)).toContainText('Irreversible')
  await expect(server.getByTestId(`op-${key}-status_incidents`)).toContainText('Read')

  // A wrong key: approved, but the check reports it broken.
  const broken = `Broken ${uid()}`
  await page.getByRole('button', { name: 'Add MCP server' }).click()
  await dialog.getByLabel('Name').fill(broken)
  await dialog.getByLabel('URL').fill('http://fakes:4100/mcp/status')
  await dialog.getByLabel('API key').fill('not-the-key')
  await dialog.getByRole('button', { name: 'Add server' }).click()
  await card(page, broken).getByRole('button', { name: 'Approve' }).click()
  await expect(card(page, broken).getByText('Broken', { exact: true })).toBeVisible()
})
