import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ConsoleGate } from '../components/Layout'
import { mockFetch, renderRoutes } from '../test/render'
import { RolesPage } from './Roles'

const me = (privilege: 'admin' | 'user') => ({
  user: { id: 'u', email: 'alice@acme.test', name: 'Alice' },
  org: { id: 'o', name: 'Acme' },
  membership: { id: 'm', privilege, roles: [] },
})
const routes = [
  { path: '/', element: <ConsoleGate />, children: [{ path: 'roles', element: <RolesPage /> }] },
]
const support = {
  id: 'r1',
  title: 'Support',
  description: 'Customers first.',
  profile: { detail: 'brief', style: 'plain', focus: ['customer_impact', 'next_steps'] },
  allowed_playbooks: ['slack_qa', 'jira_tickets'],
  blocked_operations: ['github.push_changes'],
  ask_operations: [],
  approves_operations: [],
  max_mode: 'ask',
  status: 'active',
  members: 2,
  primary_for: 1,
  created_at: '2026-10-17T10:00:00Z',
}
const options = {
  playbooks: [
    { key: 'slack_qa', title: 'Questions and summaries', description: '' },
    { key: 'jira_tickets', title: 'Jira tickets from chat', description: '' },
    { key: 'implement_change', title: 'Implement the plan', description: '' },
  ],
  operations: [
    {
      key: 'jira.search_issues',
      integration: 'jira',
      operation: 'search_issues',
      tier: 'read',
      description: '',
    },
    {
      key: 'jira.create_issue',
      integration: 'jira',
      operation: 'create_issue',
      tier: 'reversible_write',
      description: '',
    },
    {
      key: 'github.push_changes',
      integration: 'github',
      operation: 'push_changes',
      tier: 'reversible_write',
      description: '',
    },
  ],
}

afterEach(() => vi.unstubAllGlobals())

describe('Roles page', () => {
  it('shows each role’s profile as labels', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me('user') }),
        'GET /api/roles': () => ({ body: [support] }),
        'GET /api/roles/options': () => ({ body: options }),
      }).fn,
    )
    renderRoutes(routes, { path: '/roles' })
    const card = await screen.findByTestId('role-Support')
    for (const label of ['Brief', 'Plain language', 'Calls out customer impact, next steps'])
      expect(within(card).getByText(label)).toBeInTheDocument()
    for (const label of [
      'Only: Questions and summaries, Jira tickets from chat',
      'Always Ask mode',
      'Never: GitHub: push changes',
    ])
      expect(await within(card).findByText(label)).toBeInTheDocument()
    expect(within(card).getByText('Customers first.')).toBeInTheDocument()
    expect(within(card).queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
  })

  it('edits a profile and previews it before saving', async () => {
    let previewPolls = 0
    const fetch = mockFetch({
      'GET /api/me': () => ({ body: me('admin') }),
      'GET /api/roles': () => ({ body: [support] }),
      'GET /api/roles/options': () => ({ body: options }),
      'POST /api/roles/preview': () => ({
        status: 202,
        body: { id: 'role-preview-o-1', request: 'How should we fix this?' },
      }),
      'GET /api/roles/preview/role-preview-o-1': () =>
        ++previewPolls < 2
          ? { body: { status: 'running' } }
          : {
              body: {
                status: 'done',
                without: '• Totals are wrong\n• Discounts go negative\n• 40 orders',
                with_role: '*TL;DR:* two small fixes today\n• Customers were undercharged',
              },
            },
      'PATCH /api/roles/r1': () => ({ body: support }),
    })
    vi.stubGlobal('fetch', fetch.fn)
    const user = userEvent.setup()
    renderRoutes(routes, { path: '/roles' })
    await user.click(
      within(await screen.findByTestId('role-Support')).getByRole('button', { name: 'Edit' }),
    )
    const dialog = screen.getByRole('dialog', { name: 'Edit Support' })
    expect(within(dialog).getByRole('radio', { name: 'Brief' })).toBeChecked()
    await user.click(within(dialog).getByRole('radio', { name: 'Business impact' }))
    await user.click(within(dialog).getByRole('radio', { name: 'Summary first' }))
    await user.click(within(dialog).getByRole('checkbox', { name: 'Next steps' })) // off
    await user.click(within(dialog).getByRole('checkbox', { name: 'Risks' }))

    await user.selectOptions(within(dialog).getByLabelText('Sample request'), 'fix')
    await user.click(within(dialog).getByRole('button', { name: 'Preview' }))
    const panel = await within(dialog).findByTestId('preview-with-role', {}, { timeout: 4000 })
    expect(within(panel).getByText('As Support')).toBeInTheDocument()
    expect(within(panel).getByText('TL;DR:')).toHaveProperty('tagName', 'STRONG')
    expect(within(dialog).getByText('Without a role')).toBeInTheDocument()
    const started = fetch.calls.find((c) => c.method === 'POST')!
    expect(JSON.parse(String(started.init?.body))).toEqual({
      title: 'Support',
      description: 'Customers first.',
      profile: {
        detail: 'brief',
        style: 'business',
        format: 'summary_first',
        focus: ['customer_impact', 'risks'],
      },
      sample: 'fix',
      request: null,
    })

    // Limits: allow implementing too, stop always asking, ask before tickets, approve pushes.
    expect(within(dialog).getByRole('checkbox', { name: 'All playbooks' })).not.toBeChecked()
    await user.click(within(dialog).getByRole('checkbox', { name: 'Implement the plan' }))
    await user.click(within(dialog).getByRole('checkbox', { name: /Always work in Ask mode/ }))
    await user.click(
      within(dialog).getByRole('checkbox', { name: 'Always ask: Jira: create issue' }),
    )
    await user.click(within(dialog).getByRole('checkbox', { name: 'Never: GitHub: push changes' }))
    await user.click(
      within(dialog).getByRole('checkbox', { name: 'Approves: GitHub: push changes' }),
    )
    expect(
      within(dialog).queryByRole('checkbox', { name: /Always ask: Jira: search issues/ }),
    ).toBeNull()

    await user.click(within(dialog).getByRole('button', { name: 'Save role' }))
    await waitFor(() => expect(fetch.calls.some((c) => c.method === 'PATCH')).toBe(true))
    const saved = JSON.parse(String(fetch.calls.find((c) => c.method === 'PATCH')!.init?.body))
    expect(saved.profile.style).toBe('business')
    expect(saved).toMatchObject({
      allowed_playbooks: ['slack_qa', 'jira_tickets', 'implement_change'],
      max_mode: null,
      blocked_operations: [],
      ask_operations: ['jira.create_issue'],
      approves_operations: ['github.push_changes'],
    })
  })
})
