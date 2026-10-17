import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ConsoleGate } from '../components/Layout'
import { mockFetch, renderRoutes } from '../test/render'
import { SettingsPage } from './Settings'

const me = {
  user: { id: 'u', email: 'dave@globex.test', name: 'Dave' },
  org: { id: 'o', name: 'Globex' },
  membership: { id: 'm', privilege: 'admin', roles: [] },
}
const routes = [
  {
    path: '/',
    element: <ConsoleGate />,
    children: [{ path: 'settings', element: <SettingsPage /> }],
  },
]
const settings = {
  default_model_tier: 'standard',
  allowed_model_tiers: ['fast', 'standard'],
  user_daily_token_limit: 50000,
  org_monthly_token_limit: 2000000,
  max_concurrent_tasks: 3,
  max_schedules: 5,
  retention_days: 30,
  display_settings: {},
  shadow_mode: true,
  updated_at: '2026-10-16T09:00:00Z',
  plan: {
    key: 'free',
    title: 'Free',
    user_daily_token_limit: 250000,
    org_monthly_token_limit: 5000000,
    max_concurrent_tasks: 3,
    max_schedules: 5,
    max_retention_days: 90,
    model_tiers: ['fast', 'standard'],
  },
}

afterEach(() => vi.unstubAllGlobals())

describe('Settings page', () => {
  it('shows the plan ceilings and turns shadow mode off', async () => {
    let sent: Record<string, unknown> = {}
    const fetch = mockFetch({
      'GET /api/me': () => ({ body: me }),
      'GET /api/settings': () => ({ body: settings }),
      'GET /api/autonomy': () => ({ body: { org: 'ask', set: false } }),
      'PATCH /api/settings': (init) => {
        sent = JSON.parse(String(init?.body))
        return { body: { ...settings, shadow_mode: false, updated_at: '2026-10-16T10:00:00Z' } }
      },
    })
    vi.stubGlobal('fetch', fetch.fn)
    renderRoutes(routes, { path: '/settings' })
    expect(await screen.findByText('At most 250,000 on the Free plan')).toBeInTheDocument()
    expect(screen.getByText('At most 90 on the Free plan')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /Advanced/ })).toBeDisabled()
    expect(screen.getByText('(not on the Free plan)')).toBeInTheDocument()
    const shadow = screen.getByRole('checkbox', { name: /Shadow mode/ })
    expect(shadow).toBeChecked()
    await userEvent.click(shadow)
    await userEvent.click(screen.getByRole('button', { name: 'Save settings' }))
    expect(await screen.findByText('Settings saved.')).toBeInTheDocument()
    expect(sent.shadow_mode).toBe(false)
  })

  it('lets people choose Auto, saved as soon as it is chosen', async () => {
    let sent: Record<string, unknown> = {}
    let org = 'ask'
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me }),
        'GET /api/settings': () => ({ body: settings }),
        'GET /api/autonomy': () => ({ body: { org, set: org !== 'ask' } }),
        'PUT /api/autonomy': (init) => {
          sent = JSON.parse(String(init?.body))
          org = String(sent.org)
          return { body: { org, set: true } }
        },
      }).fn,
    )
    renderRoutes(routes, { path: '/settings' })
    const ask = await screen.findByRole('radio', { name: /Ask before every change/ })
    expect(ask).toBeChecked()
    await userEvent.click(screen.getByRole('radio', { name: /Let people choose Auto/ }))
    expect(await screen.findByText('Saved.')).toBeInTheDocument()
    expect(sent).toEqual({ org: 'auto' })
    expect(screen.getByRole('radio', { name: /Let people choose Auto/ })).toBeChecked()
  })

  it('shows why a value is beyond the plan', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me }),
        'GET /api/settings': () => ({ body: settings }),
        'GET /api/autonomy': () => ({ body: { org: 'ask', set: false } }),
        'PATCH /api/settings': () => ({
          status: 422,
          body: {
            error: {
              code: 'plan_limit',
              message: 'The Free plan allows at most 90 days of retention.',
            },
          },
        }),
      }).fn,
    )
    renderRoutes(routes, { path: '/settings' })
    const retention = await screen.findByLabelText('Retention (days)')
    await userEvent.clear(retention)
    await userEvent.type(retention, '120')
    await userEvent.click(screen.getByRole('button', { name: 'Save settings' }))
    expect(
      await screen.findByText('The Free plan allows at most 90 days of retention.'),
    ).toBeInTheDocument()
  })
})
