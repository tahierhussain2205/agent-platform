import { expect, test } from '@playwright/test'
import { createOrg, founder, memberRow, signIn, signUp, trackErrors, uid } from './helpers'

test('signing up creates an org and makes you its admin', async ({ page }) => {
  const errors = trackErrors(page)
  const name = `Startup ${uid()}`
  await signUp(page, `new-${uid()}@e2e.test`, 'Nia', 'New')
  await createOrg(page, name)
  await expect(page.getByText('Admin', { exact: true })).toBeVisible()
  await expect(page.getByTestId('stat-admins')).toHaveText('1')
  await expect(page.getByTestId('stat-members')).toHaveText('1')
  await expect(page.getByRole('link', { name: 'Settings' })).toBeVisible()
  expect(errors).toEqual([])
})

test('invite, accept, promote, demote, last admin rule, remove', async ({ browser }) => {
  const orgName = `Team ${uid()}`
  const admin = await founder(browser, orgName)
  const errors = trackErrors(admin.page)
  const inviteeEmail = `invitee-${uid()}@e2e.test`

  // Invite.
  await admin.page.getByRole('link', { name: 'Members' }).click()
  await admin.page.getByLabel('Email').fill(inviteeEmail)
  await admin.page.getByRole('button', { name: 'Send invitation' }).click()
  await expect(admin.page.getByRole('status')).toHaveText(`Invitation sent to ${inviteeEmail}.`)
  await expect(memberRow(admin.page, inviteeEmail).getByText('Invited')).toBeVisible()

  // The invitee signs up, sees the invitation and accepts it.
  const inviteeContext = await browser.newContext()
  const invitee = await inviteeContext.newPage()
  await signUp(invitee, inviteeEmail, 'Ivy', 'Invitee')
  await expect(invitee.getByRole('heading', { name: 'Invitations' })).toBeVisible()
  await expect(invitee.getByText(orgName)).toBeVisible()
  await invitee.getByRole('button', { name: 'Accept' }).click()
  await expect(invitee.getByRole('heading', { level: 1, name: orgName })).toBeVisible({ timeout: 20_000 })
  await expect(invitee.getByText('Member', { exact: true })).toBeVisible()
  await expect(invitee.getByRole('link', { name: 'Settings' })).toHaveCount(0)

  // The admin sees them active (the membership arrives through Clerk's webhook).
  await expect(async () => {
    await admin.page.reload()
    await expect(memberRow(admin.page, inviteeEmail).getByText('Active')).toBeVisible({ timeout: 1_000 })
  }).toPass({ timeout: 20_000 })

  // Promote, then demote.
  const row = memberRow(admin.page, inviteeEmail)
  await row.getByRole('button', { name: 'Make admin' }).click()
  await expect(row.getByText('Admin', { exact: true })).toBeVisible()
  await row.getByRole('button', { name: 'Make member' }).click()
  await expect(row.getByText('Member', { exact: true })).toBeVisible()

  // The founder is the only admin: demoting themselves is refused, with the request id shown.
  const self = memberRow(admin.page, admin.email)
  await self.getByRole('button', { name: 'Make member' }).click()
  await expect(self.getByRole('alert')).toContainText('An organization must keep at least one active admin.')
  await expect(self.getByRole('alert')).toContainText('Request ID:')

  // Remove the invitee; they lose access.
  await row.getByRole('button', { name: 'Remove' }).click()
  await admin.page.getByRole('dialog').getByRole('button', { name: 'Remove member' }).click()
  await expect(memberRow(admin.page, inviteeEmail)).toHaveCount(0)
  // Removed in Clerk too: they belong to no org now, so they are asked to set one up and cannot
  // see the team's data any more.
  await invitee.reload()
  await expect(invitee.getByRole('heading', { name: 'Set up your organization' })).toBeVisible({ timeout: 20_000 })
  await invitee.goto('/members')
  await expect(invitee.getByRole('heading', { name: 'Set up your organization' })).toBeVisible()

  expect(errors).toEqual([])
  await inviteeContext.close()
  await admin.context.close()
})

test('revoking a pending invitation', async ({ browser }) => {
  const admin = await founder(browser, `Revokers ${uid()}`)
  const email = `maybe-${uid()}@e2e.test`
  await admin.page.getByRole('link', { name: 'Members' }).click()
  await admin.page.getByLabel('Email').fill(email)
  await admin.page.getByRole('button', { name: 'Send invitation' }).click()
  await memberRow(admin.page, email).getByRole('button', { name: 'Revoke invitation' }).click()
  await expect(memberRow(admin.page, email)).toHaveCount(0)
  // Inviting the same address twice in a row is refused.
  await admin.page.getByLabel('Email').fill(email)
  await admin.page.getByRole('button', { name: 'Send invitation' }).click()
  await expect(admin.page.getByRole('status')).toHaveText(`Invitation sent to ${email}.`)
  await admin.page.getByLabel('Email').fill(email)
  await admin.page.getByRole('button', { name: 'Send invitation' }).click()
  await expect(admin.page.getByRole('alert')).toContainText('already has a pending invitation')
  await admin.context.close()
})

test('roles: assign several with one primary, change it, archive', async ({ page }) => {
  const errors = trackErrors(page)
  const lead = `Lead ${uid()}`
  const reviewer = `Reviewer ${uid()}`
  await signIn(page, 'alice@acme.test')
  await page.getByRole('link', { name: 'Roles' }).click()
  for (const title of [lead, reviewer]) {
    await page.getByRole('button', { name: 'New role' }).click()
    await page.getByLabel('Title').fill(title)
    await page.getByLabel('How should the agent respond').fill(`Respond as a ${title}.`)
    await page.getByRole('button', { name: 'Create role' }).click()
    await expect(page.getByTestId(`role-${title}`)).toBeVisible()
  }
  // Duplicate titles are refused (case insensitive).
  await page.getByRole('button', { name: 'New role' }).click()
  await page.getByLabel('Title').fill(lead.toUpperCase())
  await page.getByRole('button', { name: 'Create role' }).click()
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('conflicts')
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel' }).click()

  await page.getByRole('link', { name: 'Members' }).click()
  const quinn = memberRow(page, 'quinn@acme.test')
  await quinn.getByRole('button', { name: 'Edit roles' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('QA', { exact: true }).uncheck()
  await dialog.getByLabel(lead, { exact: true }).check()
  await dialog.getByLabel(reviewer, { exact: true }).check()
  await dialog.getByLabel(`Primary: ${reviewer}`).check()
  await dialog.getByRole('button', { name: 'Save roles' }).click()
  await expect(quinn.getByText(`${reviewer} ★`)).toBeVisible()
  await expect(quinn.getByText('QA')).toHaveCount(0)

  await page.getByRole('link', { name: 'Roles' }).click()
  await page.getByTestId(`role-${reviewer}`).getByRole('button', { name: 'Archive' }).click()
  await expect(page.getByTestId(`role-${reviewer}`)).toHaveCount(0)
  await page.getByRole('link', { name: 'Members' }).click()
  // The primary was archived: the next role was promoted to primary.
  await expect(memberRow(page, 'quinn@acme.test').getByText(`${lead} ★`)).toBeVisible()
  expect(errors).toEqual([])
})

test('settings: validation and saving', async ({ page }) => {
  await signIn(page, 'alice@acme.test')
  await page.getByRole('link', { name: 'Settings' }).click()
  await page.getByLabel('Retention (days)').fill('0')
  await page.getByRole('button', { name: 'Save settings' }).click()
  // Shown under the field, and in the form notice together with the request id.
  await expect(page.getByRole('alert').filter({ hasText: /^Input should be greater than or equal to 1$/ })).toBeVisible()
  await expect(page.getByRole('alert').filter({ hasText: 'Request ID:' })).toContainText('Input should be greater than or equal to 1')
  await page.getByLabel('Retention (days)').fill('120')
  await page.getByRole('button', { name: 'Save settings' }).click()
  await expect(page.getByText('Settings saved.')).toBeVisible()
  await page.reload()
  await expect(page.getByLabel('Retention (days)')).toHaveValue('120')
  // The default tier must stay among the allowed tiers.
  await page.getByLabel('Advanced', { exact: true }).uncheck()
  await page.getByLabel('Default tier').selectOption('advanced')
  await expect(page.getByText('The default tier must be one of the allowed tiers.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Save settings' })).toBeDisabled()
})

test('audit log shows console changes', async ({ page }) => {
  await signIn(page, 'alice@acme.test')
  await page.getByRole('link', { name: 'Members' }).click()
  const email = `audited-${uid()}@acme.test`
  await page.getByLabel('Email').fill(email)
  await page.getByRole('button', { name: 'Send invitation' }).click()
  await expect(page.getByRole('status')).toBeVisible()
  await page.getByRole('link', { name: 'Audit log' }).click()
  await page.getByLabel('Filter by action').fill('member.')
  const row = page.getByTestId('audit-row').filter({ hasText: email })
  await expect(row).toContainText('member.invited')
  await expect(row).toContainText('alice@acme.test')
})

test('members who are not admins get a read only console', async ({ page }) => {
  await signIn(page, 'bob@acme.test')
  await expect(page.getByText('Member', { exact: true })).toBeVisible()
  for (const name of ['Settings', 'Audit log']) await expect(page.getByRole('link', { name })).toHaveCount(0)
  await page.goto('/settings')
  await expect(page.getByRole('heading', { level: 1, name: 'Acme Corp' })).toBeVisible()
  await page.getByRole('link', { name: 'Members' }).click()
  await expect(memberRow(page, 'alice@acme.test')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Send invitation' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Make admin' })).toHaveCount(0)
})

test('a member of two orgs switches between them', async ({ page }) => {
  await signIn(page, 'frank@both.test')
  await expect(page.getByRole('heading', { level: 1, name: 'Acme Corp' })).toBeVisible()
  await page.getByLabel('Organization').selectOption({ label: 'Globex Inc' })
  await expect(page.getByRole('heading', { level: 1, name: 'Globex Inc' })).toBeVisible()
  await page.getByRole('link', { name: 'Members' }).click()
  await expect(memberRow(page, 'dave@globex.test')).toBeVisible()
  await expect(memberRow(page, 'alice@acme.test')).toHaveCount(0)
})

test('renaming the org and signing out', async ({ page }) => {
  await signIn(page, 'dave@globex.test')
  await page.getByRole('button', { name: 'Rename' }).click()
  await page.getByLabel('Organization name').fill('Globex Renamed')
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Globex Renamed' })).toBeVisible()
  await page.getByRole('button', { name: 'Rename' }).click()
  await page.getByLabel('Organization name').fill('Globex Inc')
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Globex Inc' })).toBeVisible()
  await page.getByRole('button', { name: 'Sign out' }).click()
  await expect(page).toHaveURL(/\/sign-in$/)
})

test('wrong password and unknown routes', async ({ page }) => {
  await page.goto('/sign-in')
  await page.getByLabel('Email').fill('alice@acme.test')
  await page.getByLabel('Password').fill('wrong-password')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('alert')).toContainText('Incorrect email or password')
  await page.goto('/definitely/not/here')
  await expect(page.getByRole('heading', { name: 'Page not found' })).toBeVisible()
})
