import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { CatalogOptions, CatalogProposal, CatalogService } from '../api/hooks'
import { ConsoleGate } from '../components/Layout'
import { mockFetch, renderRoutes } from '../test/render'
import { CatalogPage } from './Catalog'

const me = (privilege: string) => ({
  user: { id: 'u', email: 'alice@acme.test', name: 'Alice' },
  org: { id: 'o', name: 'Acme' },
  membership: { id: 'm-alice', privilege, roles: [] },
})
const routes = [
  {
    path: '/',
    element: <ConsoleGate />,
    children: [{ path: 'catalog', element: <CatalogPage /> }],
  },
]
const options: CatalogOptions = {
  members: [
    { membership_id: 'm-bob', email: 'bob@acme.test', name: 'Bob Builder' },
    { membership_id: 'm-quinn', email: 'quinn@acme.test', name: 'Quinn Tester' },
  ],
  channels: [{ id: 'c-bugs', name: 'bugs' }],
  repositories: [{ id: 'r-cart', full_name: 'acme/cart-service' }],
}

function service(overrides: Partial<CatalogService> = {}): CatalogService {
  return {
    id: 's-cart',
    name: 'cart-service',
    description: 'Prices carts',
    status: 'active',
    repository: { id: 'r-cart', full_name: 'acme/cart-service' },
    jira_project: 'PAY',
    jira_component: 'Checkout',
    paths: ['/src/pricing/'],
    evidence: [],
    confirmed_at: '2026-10-16T00:00:00Z',
    confirmed_by: 'alice@acme.test',
    created_at: '2026-10-16T00:00:00Z',
    owners: [
      {
        membership_id: 'm-bob',
        email: 'bob@acme.test',
        name: 'Bob Builder',
        source: 'codeowners',
        reason: 'CODEOWNERS /src/pricing/ in acme/cart-service',
        active: true,
      },
    ],
    channels: [{ id: 'c-bugs', name: 'bugs' }],
    ...overrides,
  }
}

const proposedRefunds = service({
  id: 's-refunds',
  name: 'Refunds',
  status: 'proposed',
  repository: null,
  jira_component: 'Refunds',
  paths: [],
  channels: [],
  owners: [],
  evidence: ['Jira component PAY/Refunds: Jira account acc-zed (no member matched)'],
})

function stub(privilege: string, services: CatalogService[], extra = {}) {
  const mock = mockFetch({
    'GET /api/me': () => ({ body: me(privilege) }),
    'GET /api/catalog': () => ({
      body: { services, can_manage: privilege === 'admin', last_proposal: null },
    }),
    'GET /api/catalog/options': () => ({ body: options }),
    'GET /api/catalog/proposal': () => ({ body: { status: 'none' } }),
    ...extra,
  })
  vi.stubGlobal('fetch', mock.fn)
  return mock
}

afterEach(() => vi.unstubAllGlobals())

describe('Catalog page', () => {
  it('shows members who owns what and why, without controls', async () => {
    stub('user', [service()])
    renderRoutes(routes, { path: '/catalog' })
    const row = await screen.findByRole('listitem', { name: 'cart-service' })
    expect(within(row).getByText('Confirmed')).toBeInTheDocument()
    expect(
      within(row).getByText(/Repository acme\/cart-service · Jira PAY \/ Checkout/),
    ).toBeInTheDocument()
    const owners = within(row).getByRole('list', { name: 'Owners' })
    expect(within(owners).getByText('Bob Builder')).toBeInTheDocument()
    expect(within(owners).getByText('CODEOWNERS')).toBeInTheDocument()
    expect(
      within(owners).getByText('CODEOWNERS /src/pricing/ in acme/cart-service'),
    ).toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Propose services' })).not.toBeInTheDocument()
  })

  it('lists proposals first with their evidence and lets admins confirm them', async () => {
    let confirmed = false
    const mock = stub('admin', [service()], {
      'GET /api/catalog': () => ({
        body: {
          services: [
            confirmed ? { ...proposedRefunds, status: 'active' } : proposedRefunds,
            service(),
          ],
          can_manage: true,
          last_proposal: null,
        },
      }),
      'POST /api/catalog/s-refunds/confirm': () => {
        confirmed = true
        return { body: {} }
      },
    })
    renderRoutes(routes, { path: '/catalog' })
    const proposed = await screen.findByRole('list', { name: 'Proposed services' })
    const row = within(proposed).getByRole('listitem', { name: 'Refunds' })
    expect(within(row).getByText('No owners yet')).toBeInTheDocument()
    expect(within(row).getByText(/acc-zed \(no member matched\)/)).toBeInTheDocument()
    await userEvent.click(within(row).getByRole('button', { name: 'Confirm' }))
    await waitFor(() =>
      expect(screen.queryByRole('list', { name: 'Proposed services' })).not.toBeInTheDocument(),
    )
    const all = screen.getByRole('list', { name: 'Confirmed services' })
    expect(within(all).getByRole('listitem', { name: 'Refunds' })).toBeInTheDocument()
    expect(
      mock.calls.some((c) => c.method === 'POST' && c.url.endsWith('/s-refunds/confirm')),
    ).toBe(true)
  })

  it('proposes services and shows the result when the run finishes', async () => {
    let status: CatalogProposal = { status: 'none' }
    let services = [service()]
    stub('admin', services, {
      'GET /api/catalog': () => ({ body: { services, can_manage: true, last_proposal: null } }),
      'POST /api/catalog/propose': () => {
        status = { status: 'running' }
        setTimeout(() => {
          status = {
            status: 'completed',
            proposed: 1,
            refreshed: 0,
            linked: 1,
            kept: 0,
            skipped: 0,
            notes: ['GitHub repositories: GitHub is not connected for this organization.'],
          }
          services = [proposedRefunds, service()]
        }, 300)
        return { status: 202, body: { status: 'running' } }
      },
      'GET /api/catalog/proposal': () => ({ body: status }),
    })
    renderRoutes(routes, { path: '/catalog' })
    await userEvent.click(await screen.findByRole('button', { name: 'Propose services' }))
    expect(await screen.findByText('Proposing services from GitHub and Jira…')).toBeInTheDocument()
    expect(
      await screen.findByText(
        'Last proposal: 1 proposed, 1 confirmed services linked.',
        {},
        { timeout: 4000 },
      ),
    ).toBeInTheDocument()
    expect(screen.getByText(/GitHub is not connected/)).toBeInTheDocument()
    expect(await screen.findByRole('listitem', { name: 'Refunds' })).toBeInTheDocument()
  })

  it('edits owners and links', async () => {
    let sent: unknown = null
    stub('admin', [service()], {
      'PATCH /api/catalog/s-cart': (init?: RequestInit) => {
        sent = JSON.parse(String(init?.body))
        return { body: {} }
      },
    })
    renderRoutes(routes, { path: '/catalog' })
    const row = await screen.findByRole('listitem', { name: 'cart-service' })
    await userEvent.click(within(row).getByRole('button', { name: 'Edit' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.click(within(dialog).getByLabelText('Quinn Tester (quinn@acme.test)'))
    await userEvent.click(within(dialog).getByLabelText('Bob Builder (bob@acme.test)'))
    await userEvent.clear(within(dialog).getByLabelText('Jira component'))
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(sent).not.toBeNull())
    expect(sent).toMatchObject({
      name: 'cart-service',
      repository_id: 'r-cart',
      jira_project: 'PAY',
      jira_component: '',
      paths: ['/src/pricing/'],
      owners: ['m-quinn'],
      channels: ['c-bugs'],
    })
  })

  it('adds a service and shows why a save was refused', async () => {
    stub('admin', [], {
      'POST /api/catalog': () => ({
        status: 409,
        body: {
          error: { code: 'name_taken', message: 'Another service in the catalog has this name.' },
        },
      }),
    })
    renderRoutes(routes, { path: '/catalog' })
    expect(await screen.findByText(/No confirmed services yet/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Add service' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.type(within(dialog).getByLabelText('Name'), 'cart-service')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(
      await within(dialog).findByText('Another service in the catalog has this name.'),
    ).toBeInTheDocument()
  })
})
