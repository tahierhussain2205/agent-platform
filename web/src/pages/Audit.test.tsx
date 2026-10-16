import { screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ConsoleGate } from '../components/Layout'
import { mockFetch, renderRoutes } from '../test/render'
import { AuditPage } from './Audit'

const me = {
  user: { id: 'u', email: 'alice@acme.test', name: 'Alice' },
  org: { id: 'o', name: 'Acme' },
  membership: { id: 'm', privilege: 'admin', roles: [] },
}
const routes = [
  { path: '/', element: <ConsoleGate />, children: [{ path: 'audit', element: <AuditPage /> }] },
]
const entry = (overrides: Record<string, unknown>) => ({
  id: crypto.randomUUID(),
  occurred_at: '2026-10-16T09:00:00Z',
  actor_type: 'user',
  actor_email: 'alice@acme.test',
  action: 'settings.updated',
  target: null,
  decision: 'info',
  reason: null,
  metadata: {},
  ...overrides,
})

afterEach(() => vi.unstubAllGlobals())

describe('Audit page', () => {
  it('shows agent decisions on behalf of the requester, with the reason', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me }),
        'GET /api/audit': () => ({
          body: {
            items: [
              entry({
                actor_type: 'agent',
                actor_email: 'bob@acme.test',
                action: 'policy.tool',
                target: 'slack.read_channel #leads-private',
                decision: 'denied',
                reason: 'You are not a member of #leads-private, so I can’t use it for you.',
              }),
              entry({ actor_type: 'system', actor_email: null, action: 'task.reconciled' }),
              entry({}),
            ],
            next_before: null,
          },
        }),
      }).fn,
    )
    renderRoutes(routes, { path: '/audit' })
    expect(await screen.findByText('Agent for bob@acme.test')).toBeInTheDocument()
    expect(screen.getByText('slack.read_channel #leads-private')).toBeInTheDocument()
    expect(screen.getByText(/You are not a member of #leads-private/)).toBeInTheDocument()
    expect(screen.getByText('denied')).toBeInTheDocument()
    expect(screen.getByText('system')).toBeInTheDocument()
    expect(screen.getAllByText('alice@acme.test').length).toBeGreaterThan(0)
  })
})
