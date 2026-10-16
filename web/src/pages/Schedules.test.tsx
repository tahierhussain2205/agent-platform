import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Schedule, ScheduleOptions } from '../api/hooks'
import { ConsoleGate } from '../components/Layout'
import { mockFetch, renderRoutes } from '../test/render'
import { fromCron, toCron } from './cadence'
import { SchedulesPage } from './Schedules'

const me = (privilege: string) => ({
  user: { id: 'u', email: 'bob@acme.test', name: 'Bob' },
  org: { id: 'o', name: 'Acme' },
  membership: { id: 'm-bob', privilege, roles: [] },
})
const routes = [
  {
    path: '/',
    element: <ConsoleGate />,
    children: [{ path: 'schedules', element: <SchedulesPage /> }],
  },
]

const options: ScheduleOptions = {
  channels: [
    { id: 'c-bugs', name: 'bugs', is_private: false },
    { id: 'c-general', name: 'general', is_private: false },
  ],
  delivery_channels: [{ id: 'c-general', name: 'general' }],
  jira_enabled: true,
  has_slack_account: true,
  timezone: 'Europe/Berlin',
}

function schedule(overrides: Partial<Schedule> = {}): Schedule {
  return {
    id: 's1',
    name: 'Daily bugs',
    kind: 'channel_summary',
    channels: [{ id: 'c-bugs', name: 'bugs' }],
    project: null,
    cron: '0 9 * * 1-5',
    timezone: 'Europe/Berlin',
    description: 'Every weekday at 09:00',
    next_runs: ['2026-10-19T07:00:00+00:00'],
    delivery: { type: 'dm', channel_id: null, channel_name: null },
    status: 'active',
    pause_reason: null,
    pause_detail: null,
    paused_at: null,
    last_run_at: null,
    created_at: '2026-10-16T00:00:00Z',
    owner: { membership_id: 'm-bob', email: 'bob@acme.test', name: 'Bob', status: 'active' },
    can_manage: true,
    ...overrides,
  }
}

function stub(privilege: string, schedules: Schedule[], extra = {}) {
  const mock = mockFetch({
    'GET /api/me': () => ({ body: me(privilege) }),
    'GET /api/schedules': () => ({ body: { schedules, limits: { max_schedules: 25, active: 1 } } }),
    'GET /api/schedules/options': () => ({ body: options }),
    'POST /api/schedules/preview': () => ({
      body: { cron: '0 9 * * 1-5', description: 'Every weekday at 09:00', next_runs: [] },
    }),
    ...extra,
  })
  vi.stubGlobal('fetch', mock.fn)
  return mock
}

afterEach(() => vi.unstubAllGlobals())

describe('cadence helpers', () => {
  it('turns presets into cron and back', () => {
    expect(toCron({ preset: 'weekdays', time: '08:30', day: '1', cron: '' })).toBe('30 8 * * 1-5')
    expect(toCron({ preset: 'daily', time: '17:05', day: '1', cron: '' })).toBe('5 17 * * *')
    expect(toCron({ preset: 'weekly', time: '10:00', day: '3', cron: '' })).toBe('0 10 * * 3')
    expect(toCron({ preset: 'custom', time: '', day: '', cron: ' 0 9 1 * * ' })).toBe('0 9 1 * *')
    expect(fromCron('30 8 * * 1-5')).toMatchObject({ preset: 'weekdays', time: '08:30' })
    expect(fromCron('0 10 * * 3')).toMatchObject({ preset: 'weekly', day: '3', time: '10:00' })
    expect(fromCron('0 9 1 * *')).toMatchObject({ preset: 'custom', cron: '0 9 1 * *' })
  })
})

describe('Schedules page', () => {
  it('lists a schedule with its cadence, next run and controls', async () => {
    stub('user', [schedule()])
    renderRoutes(routes, { path: '/schedules' })
    const row = await screen.findByRole('listitem', { name: 'Daily bugs' })
    expect(within(row).getByText('Summary of #bugs')).toBeInTheDocument()
    expect(within(row).getByText(/Every weekday at 09:00 \(Europe\/Berlin\)/)).toBeInTheDocument()
    expect(within(row).getByText(/Next run: Mon 19 Oct, 09:00/)).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: 'Pause' })).toBeEnabled()
    expect(within(row).queryByRole('button', { name: 'Reassign' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('schedule-limits')).not.toBeInTheDocument()
  })

  it('shows why a schedule paused and lets admins reassign it', async () => {
    let owner = 'bob@acme.test'
    const mock = mockFetch({
      'GET /api/me': () => ({ body: me('admin') }),
      'GET /api/schedules': () => ({
        body: {
          schedules: [
            schedule({
              status: 'paused',
              pause_reason: 'owner_removed',
              pause_detail: 'the owner is no longer an active member',
              next_runs: [],
              owner: { membership_id: 'm-bob', email: owner, name: null, status: 'removed' },
            }),
          ],
          limits: { max_schedules: 25, active: 0 },
        },
      }),
      'GET /api/schedules/options': () => ({ body: options }),
      'GET /api/members': () => ({
        body: [
          { id: 'm-quinn', email: 'quinn@acme.test', status: 'active' },
          { id: 'm-bob', email: 'bob@acme.test', status: 'removed' },
        ],
      }),
      'POST /api/schedules/s1/reassign': (init) => {
        expect(JSON.parse(String(init?.body))).toEqual({ owner_membership_id: 'm-quinn' })
        owner = 'quinn@acme.test'
        return { body: {} }
      },
    })
    vi.stubGlobal('fetch', mock.fn)
    renderRoutes(routes, { path: '/schedules' })
    const row = await screen.findByRole('listitem', { name: 'Daily bugs' })
    expect(within(row).getByText('Owner removed')).toBeInTheDocument()
    expect(within(row).getByText('the owner is no longer an active member')).toBeInTheDocument()
    expect(screen.getByTestId('schedule-limits')).toHaveTextContent('0 of 25 active schedules')
    await userEvent.click(within(row).getByRole('button', { name: 'Reassign' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).queryByRole('option', { name: 'bob@acme.test' })).not.toBeInTheDocument()
    await userEvent.selectOptions(within(dialog).getByLabelText('New owner'), 'm-quinn')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Reassign' }))
    await waitFor(() => expect(within(row).getByText(/owner quinn@acme.test/)).toBeInTheDocument())
  })

  it('creates a Jira progress report delivered to a channel', async () => {
    let sent: Record<string, unknown> | null = null
    const mock = stub('user', [], {
      'POST /api/schedules': (init?: RequestInit) => {
        sent = JSON.parse(String(init?.body)) as Record<string, unknown>
        return { status: 201, body: schedule() }
      },
    })
    renderRoutes(routes, { path: '/schedules' })
    await userEvent.click(await screen.findByRole('button', { name: 'New schedule' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.type(within(dialog).getByLabelText('Name'), 'PAY daily')
    await userEvent.click(within(dialog).getByLabelText(/Jira progress/))
    await userEvent.type(within(dialog).getByLabelText('Jira project key'), 'pay')
    await userEvent.selectOptions(within(dialog).getByLabelText('Cadence'), 'daily')
    await userEvent.clear(within(dialog).getByLabelText('Time'))
    await userEvent.type(within(dialog).getByLabelText('Time'), '08:30')
    await userEvent.click(within(dialog).getByLabelText('Post in a channel'))
    await userEvent.selectOptions(within(dialog).getByLabelText('Delivery channel'), 'c-general')
    expect(await within(dialog).findByTestId('cadence-preview')).toHaveTextContent(
      'Every weekday at 09:00',
    )
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create schedule' }))
    await waitFor(() => expect(sent).not.toBeNull())
    expect(sent).toEqual({
      name: 'PAY daily',
      kind: 'jira_progress',
      cron: '30 8 * * *',
      timezone: 'Europe/Berlin',
      delivery: 'channel',
      channels: [],
      project: 'PAY',
      delivery_channel_id: 'c-general',
    })
    expect(mock.calls.some((c) => c.url.endsWith('/api/schedules/preview'))).toBe(true)
  })

  it('shows the API refusal when a save is not valid', async () => {
    stub('user', [], {
      'POST /api/schedules': () => ({
        status: 422,
        body: {
          error: {
            code: 'channel_access',
            message: "The owner can't read #general through the agent.",
          },
        },
      }),
    })
    renderRoutes(routes, { path: '/schedules' })
    await userEvent.click(await screen.findByRole('button', { name: 'New schedule' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.type(within(dialog).getByLabelText('Name'), 'General')
    await userEvent.click(within(dialog).getByLabelText('Summarize #general'))
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create schedule' }))
    expect(
      await within(dialog).findByText("The owner can't read #general through the agent."),
    ).toBeInTheDocument()
  })

  it('pauses, runs now and deletes after confirming', async () => {
    let status: Schedule['status'] = 'active'
    let deleted = false
    const mock = mockFetch({
      'GET /api/me': () => ({ body: me('user') }),
      'GET /api/schedules': () => ({
        body: {
          schedules: deleted
            ? []
            : [schedule({ status, pause_reason: status === 'paused' ? 'admin' : null })],
          limits: null,
        },
      }),
      'GET /api/schedules/options': () => ({ body: options }),
      'POST /api/schedules/s1/run': () => ({ status: 202, body: { status: 'started' } }),
      'POST /api/schedules/s1/pause': () => {
        status = 'paused'
        return { body: {} }
      },
      'DELETE /api/schedules/s1': () => {
        deleted = true
        return { status: 204 }
      },
    })
    vi.stubGlobal('fetch', mock.fn)
    renderRoutes(routes, { path: '/schedules' })
    const row = await screen.findByRole('listitem', { name: 'Daily bugs' })
    await userEvent.click(within(row).getByRole('button', { name: 'Run now' }))
    expect(await within(row).findByText('Running now: the report follows shortly.')).toBeVisible()
    await userEvent.click(within(row).getByRole('button', { name: 'Pause' }))
    expect(await within(row).findByRole('button', { name: 'Resume' })).toBeEnabled()
    await userEvent.click(within(row).getByRole('button', { name: 'Delete' }))
    expect(mock.calls.some((c) => c.method === 'DELETE')).toBe(false)
    const dialog = await screen.findByRole('dialog')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Delete' }))
    expect(await screen.findByText(/No scheduled reports yet/)).toBeInTheDocument()
  })

  it('asks people without a Slack account to say hello first', async () => {
    const mock = mockFetch({
      'GET /api/me': () => ({ body: me('user') }),
      'GET /api/schedules': () => ({ body: { schedules: [], limits: null } }),
      'GET /api/schedules/options': () => ({ body: { ...options, has_slack_account: false } }),
    })
    vi.stubGlobal('fetch', mock.fn)
    renderRoutes(routes, { path: '/schedules' })
    expect(await screen.findByRole('button', { name: 'New schedule' })).toBeDisabled()
    expect(screen.getByText(/Say hello to the agent in Slack first/)).toBeInTheDocument()
  })
})
