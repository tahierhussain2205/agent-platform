import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ConsoleGate } from '../components/Layout'
import { mockFetch, renderRoutes } from '../test/render'
import { UsagePage } from './Usage'

const me = {
  user: { id: 'u', email: 'alice@acme.test', name: 'Alice' },
  org: { id: 'o', name: 'Acme' },
  membership: { id: 'm', privilege: 'admin', roles: [] },
}
const routes = [
  { path: '/', element: <ConsoleGate />, children: [{ path: 'usage', element: <UsagePage /> }] },
]
const usage = (days: number) => ({
  days,
  limits: { user_daily_token_limit: 250000, org_monthly_token_limit: 5000000 },
  today: { tokens: 1400, cost: 0.75 },
  month: { tokens: 4500000, cost: 12.5 },
  daily: Array.from({ length: days }, (_, i) => ({
    day: `2026-10-${String(i + 1).padStart(2, '0')}`,
    tokens: i === days - 1 ? 1400 : 0,
    cost: 0,
  })),
  people: [
    {
      membership_id: 'm1',
      email: 'bob@acme.test',
      name: 'Bob',
      tokens: 3000,
      cost: 1.25,
      today_tokens: 1400,
    },
    { membership_id: null, email: null, name: null, tokens: 500, cost: 0.1, today_tokens: 0 },
  ],
  playbooks: [
    { key: 'investigate', title: 'Investigate an issue', tasks: 2, tokens: 3000, cost: 1.2 },
  ],
  models: [
    {
      model: 'claude-sonnet-5',
      input_tokens: 2500,
      output_tokens: 500,
      cached_tokens: 100,
      sandbox_seconds: 0,
      cost: 1.25,
    },
  ],
})

afterEach(() => vi.unstubAllGlobals())

describe('Usage page', () => {
  it('shows the month against the limit, people, playbooks and models', async () => {
    const fetch = mockFetch({
      'GET /api/me': () => ({ body: me }),
      'GET /api/usage?days=30': () => ({ body: usage(30) }),
      'GET /api/usage?days=7': () => ({ body: usage(7) }),
    })
    vi.stubGlobal('fetch', fetch.fn)
    renderRoutes(routes, { path: '/usage' })
    expect(await screen.findByText('of 5,000,000')).toBeInTheDocument()
    expect(screen.getByRole('meter', { name: 'Tokens used this month' })).toHaveAttribute(
      'aria-valuenow',
      '4500000',
    )
    expect(screen.getByTestId('today-tokens')).toHaveTextContent('1,400 tokens')
    expect(screen.getAllByTestId('usage-day')).toHaveLength(30)
    const people = screen.getByRole('heading', { name: 'People' }).closest('section')!
    expect(within(people).getByText('Bob (bob@acme.test)')).toBeInTheDocument()
    expect(within(people).getByText('1,400 of 250,000')).toBeInTheDocument()
    expect(within(people).getByText('Scheduled and system work')).toBeInTheDocument()
    expect(screen.getByText('Investigate an issue')).toBeInTheDocument()
    expect(screen.getByText('claude-sonnet-5')).toBeInTheDocument()
    expect(screen.getByText('$12.50 so far')).toBeInTheDocument()

    await userEvent.selectOptions(screen.getByLabelText('Window'), '7')
    expect(await screen.findAllByTestId('usage-day')).toHaveLength(7)
    expect(fetch.calls.some((c) => c.url.endsWith('/api/usage?days=7'))).toBe(true)
  })

  it('says when there is no limit or no usage', async () => {
    const empty = {
      ...usage(30),
      limits: { user_daily_token_limit: null, org_monthly_token_limit: null },
      daily: usage(30).daily.map((d) => ({ ...d, tokens: 0 })),
      people: [],
      playbooks: [],
      models: [],
    }
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me }),
        'GET /api/usage?days=30': () => ({ body: empty }),
      }).fn,
    )
    renderRoutes(routes, { path: '/usage' })
    expect(await screen.findByText('no limit')).toBeInTheDocument()
    expect(screen.getByText('No usage in this window.')).toBeInTheDocument()
    expect(screen.getByText('Nobody has used the agent in this window.')).toBeInTheDocument()
    expect(screen.getByText('No model calls in this window.')).toBeInTheDocument()
  })
})
