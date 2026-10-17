import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ConsoleGate } from '../components/Layout'
import { mockFetch, renderRoutes } from '../test/render'
import { TaskDetailPage, TasksPage } from './Tasks'

const me = (privilege: 'admin' | 'user') => ({
  user: { id: 'u', email: 'alice@acme.test', name: 'Alice' },
  org: { id: 'o', name: 'Acme' },
  membership: { id: 'm', privilege, roles: [] },
})
const routes = [
  {
    path: '/',
    element: <ConsoleGate />,
    children: [
      { path: 'tasks', element: <TasksPage /> },
      { path: 'tasks/:taskId', element: <TaskDetailPage /> },
    ],
  },
]

const task = {
  id: 't1',
  status: 'completed',
  trigger: 'mention',
  mode: 'ask',
  tier: 'standard',
  tokens_used: 12345,
  cost: '0.12',
  created_at: '2026-10-17T10:00:00Z',
  started_at: '2026-10-17T10:00:00Z',
  finished_at: '2026-10-17T10:01:05Z',
  playbook: 'jira_tickets',
  playbook_title: 'Create Jira tickets',
  requester: 'bob@acme.test',
  requester_name: 'Bob',
  channel: 'eng',
  dm: false,
  title: 'File the two checkout bugs',
  pending_approvals: 0,
  error: null,
}

const detail = {
  task: { ...task, status: 'awaiting_approval', slack_url: 'https://app.slack.com/client/T1/C1' },
  steps: [
    {
      kind: 'gather',
      status: 'completed',
      summary: 'Read #eng',
      started_at: '2026-10-17T10:00:00Z',
      finished_at: '2026-10-17T10:00:10Z',
    },
    {
      kind: 'act',
      status: 'running',
      summary: 'Create KAN-7',
      started_at: '2026-10-17T10:00:20Z',
      finished_at: null,
    },
  ],
  approvals: [
    {
      id: 'a1',
      created_at: '2026-10-17T10:00:30Z',
      action_summary: 'Create KAN-8 in Jira',
      status: 'pending',
      decided_by: null,
      decided_at: null,
      expires_at: '2026-10-18T10:00:30Z',
      decides: 'a Tech Lead or an admin',
    },
  ],
  links: [
    { kind: 'jira_issue', label: 'KAN-7', url: 'https://acme.atlassian.net/browse/KAN-7' },
    { kind: 'pull_request', label: 'acme/api#4', url: null },
  ],
  usage: { calls: 3, input_tokens: 1000, output_tokens: 200 },
  events: [
    {
      at: '2026-10-17T10:00:01Z',
      kind: 'model',
      title: 'Model call: classify',
      detail: 'm',
      tone: 'neutral',
      input_tokens: 100,
      output_tokens: 10,
    },
    {
      at: '2026-10-17T10:00:02Z',
      kind: 'model',
      title: 'Model call: brief',
      detail: 'm',
      tone: 'neutral',
      input_tokens: 400,
      output_tokens: 90,
    },
    {
      at: '2026-10-17T10:00:05Z',
      kind: 'policy',
      title: 'Policy: jira.search_issues',
      detail: 'Read access.',
      tone: 'green',
      read: true,
      target: 'jira.search_issues',
    },
    {
      at: '2026-10-17T10:00:06Z',
      kind: 'policy',
      title: 'Policy: jira.search_issues',
      detail: 'Read access.',
      tone: 'green',
      read: true,
      target: 'jira.search_issues',
    },
    {
      at: '2026-10-17T10:00:20Z',
      kind: 'policy',
      title: 'Policy: jira.create_issue',
      detail: 'Ask mode: reversible writes need approval.',
      tone: 'amber',
    },
    {
      at: '2026-10-17T10:00:30Z',
      kind: 'approval',
      title: 'Approval requested',
      detail: 'Create KAN-8 in Jira',
      tone: 'amber',
      decides: 'a Tech Lead or an admin',
      status: 'pending',
    },
    {
      at: '2026-10-17T10:00:40Z',
      kind: 'action',
      title: 'jira.create_issue',
      detail: 'jira:KAN-7',
      tone: 'green',
      status: 'succeeded',
    },
  ],
}

afterEach(() => vi.unstubAllGlobals())

describe('Tasks page', () => {
  it('lists tasks with status, requester and tokens, and filters by status', async () => {
    const fetch = mockFetch({
      'GET /api/me': () => ({ body: me('admin') }),
      'GET /api/tasks?limit=25': () => ({ body: { items: [task], next_before: null } }),
      'GET /api/tasks?limit=25&status=failed': () => ({ body: { items: [], next_before: null } }),
    })
    vi.stubGlobal('fetch', fetch.fn)
    renderRoutes(routes, { path: '/tasks' })
    const row = await screen.findByTestId('task-row')
    expect(within(row).getByRole('link', { name: 'File the two checkout bugs' })).toHaveAttribute(
      'href',
      '/tasks/t1',
    )
    expect(within(row).getByText('Create Jira tickets · #eng · Ask')).toBeInTheDocument()
    expect(within(row).getByText('Bob')).toBeInTheDocument()
    expect(within(row).getByText('Completed')).toBeInTheDocument()
    expect(within(row).getByText('1m 5s')).toBeInTheDocument()
    expect(within(row).getByText('12,345')).toBeInTheDocument()

    await userEvent.selectOptions(screen.getByLabelText('Filter by status'), 'failed')
    expect(await screen.findByText(/No tasks yet/)).toBeInTheDocument()
  })

  it('shows members their own tasks without a requester column', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me('user') }),
        'GET /api/tasks?limit=25': () => ({ body: { items: [task], next_before: null } }),
      }).fn,
    )
    renderRoutes(routes, { path: '/tasks' })
    expect(await screen.findByTestId('task-row')).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Requested by' })).not.toBeInTheDocument()
    expect(screen.getByText(/The requests you asked the agent for/)).toBeInTheDocument()
  })
})

describe('Task detail', () => {
  it('shows the steps, links, pending approval and a timeline with model calls grouped', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me('admin') }),
        'GET /api/tasks/t1': () => ({ body: detail }),
      }).fn,
    )
    renderRoutes(routes, { path: '/tasks/t1' })
    expect(
      await screen.findByRole('heading', { name: 'File the two checkout bugs' }),
    ).toBeInTheDocument()
    expect(screen.getByText('Waiting for approval', { selector: 'span' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(
      'Waiting for approval: Create KAN-8 in Jira (a Tech Lead or an admin decides)',
    )
    expect(screen.getByRole('link', { name: 'Open in Slack ↗' })).toHaveAttribute(
      'href',
      'https://app.slack.com/client/T1/C1',
    )
    expect(screen.getByRole('link', { name: 'Jira KAN-7 ↗' })).toHaveAttribute(
      'href',
      'https://acme.atlassian.net/browse/KAN-7',
    )
    expect(screen.getByText('Pull request acme/api#4')).toBeInTheDocument()

    const steps = screen.getByRole('list', { name: 'Steps' })
    expect(within(steps).getByText('Read #eng')).toBeInTheDocument()
    expect(within(steps).getAllByText('Not needed')).toHaveLength(3)

    const timeline = screen.getByRole('list', { name: 'Timeline' })
    expect(within(timeline).getAllByTestId('timeline-model')).toHaveLength(1)
    expect(
      within(timeline).getByText('2 model calls: choose a playbook, write the brief · 600 tokens'),
    ).toBeInTheDocument()
    expect(within(timeline).getByText('2 allowed reads: jira.search_issues ×2')).toBeInTheDocument()
    expect(within(timeline).getByText('Policy: jira.create_issue')).toBeInTheDocument()
    expect(within(timeline).getByText('A Tech Lead or an admin decides.')).toBeInTheDocument()
    expect(within(timeline).getByText('jira:KAN-7')).toBeInTheDocument()
    expect(screen.getByText('Updating while the task runs')).toBeInTheDocument()
  })

  it('says when the task is not there', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me('user') }),
        'GET /api/tasks/nope': () => ({
          status: 404,
          body: { error: { code: 'not_found', message: 'No such task.' } },
        }),
      }).fn,
    )
    renderRoutes(routes, { path: '/tasks/nope' })
    expect(await screen.findByText(/No such task/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '← All tasks' })).toBeInTheDocument()
  })
})
