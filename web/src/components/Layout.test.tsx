import { screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fakeAuth, mockFetch, renderRoutes } from '../test/render'
import { ConsoleGate } from './Layout'

const routes = [
  {
    path: '/',
    element: <ConsoleGate />,
    children: [{ index: true, element: <p>console home</p> }],
  },
  { path: '/sign-in', element: <p>sign in page</p> },
  { path: '/create-org', element: <p>create org page</p> },
  { path: '/invitations', element: <p>invitations page</p> },
]

const me = {
  user: { id: 'u', email: 'alice@acme.test', name: 'Alice' },
  org: { id: 'o', name: 'Acme Corp' },
  membership: { id: 'm', privilege: 'admin', roles: [] },
}

afterEach(() => vi.unstubAllGlobals())

describe('ConsoleGate', () => {
  it('sends signed out visitors to sign in without calling the API', async () => {
    const { fn, calls } = mockFetch({})
    vi.stubGlobal('fetch', fn)
    renderRoutes(routes, {
      auth: fakeAuth({ status: 'signed_out', user: null, orgs: [], activeOrgId: null }),
    })
    expect(await screen.findByText('sign in page')).toBeInTheDocument()
    expect(calls).toHaveLength(0)
  })

  it('sends people without an org to create one, or to their invitations', async () => {
    vi.stubGlobal('fetch', mockFetch({}).fn)
    const { unmount } = renderRoutes(routes, { auth: fakeAuth({ orgs: [], activeOrgId: null }) })
    expect(await screen.findByText('create org page')).toBeInTheDocument()
    unmount()
    renderRoutes(routes, {
      auth: fakeAuth({
        orgs: [],
        activeOrgId: null,
        invitations: [{ id: 'i', organizationName: 'Acme', role: 'org:member' }],
      }),
    })
    expect(await screen.findByText('invitations page')).toBeInTheDocument()
  })

  it('renders the console for members, with privilege from the API', async () => {
    vi.stubGlobal('fetch', mockFetch({ 'GET /api/me': () => ({ body: me }) }).fn)
    renderRoutes(routes)
    expect(await screen.findByText('console home')).toBeInTheDocument()
    expect(screen.getByText('Admin')).toBeInTheDocument()
    // Desktop and mobile navigation both render in jsdom (no CSS): check the main one.
    expect(
      within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', {
        name: 'Audit log',
      }),
    ).toBeInTheDocument()
  })

  it('hides admin areas from members', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({
          body: { ...me, membership: { ...me.membership, privilege: 'user' } },
        }),
      }).fn,
    )
    renderRoutes(routes)
    expect(await screen.findByText('console home')).toBeInTheDocument()
    expect(screen.queryAllByRole('link', { name: 'Settings' })).toHaveLength(0)
    expect(screen.queryAllByRole('link', { name: 'Audit log' })).toHaveLength(0)
  })

  it('explains a refusal and offers the other orgs', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({
          status: 403,
          body: { error: { code: 'user_disabled', message: 'disabled' } },
        }),
      }).fn,
    )
    renderRoutes(routes, {
      auth: fakeAuth({
        orgs: [
          { id: 'org_acme', name: 'Acme Corp', role: 'org:member' },
          { id: 'org_globex', name: 'Globex Inc', role: 'org:member' },
        ],
      }),
    })
    expect(await screen.findByRole('heading', { name: 'No access' })).toBeInTheDocument()
    expect(screen.getByText('Your account is disabled.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Switch to Globex Inc' })).toBeInTheDocument()
  })

  it('shows other errors with a retry', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({
          status: 503,
          headers: { 'x-request-id': 'req-9' },
          body: { error: { code: 'unavailable', message: 'The database is unavailable.' } },
        }),
      }).fn,
    )
    renderRoutes(routes)
    expect(await screen.findByText('The database is unavailable.')).toBeInTheDocument()
    expect(screen.getByText('Request ID: req-9')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })
})
