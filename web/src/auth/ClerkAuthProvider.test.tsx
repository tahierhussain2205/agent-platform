import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ConsoleGate } from '../components/Layout'
import { mockFetch } from '../test/render'
import { ClerkBridge } from './ClerkAuthProvider'

// What Clerk's hooks report; each test sets the moment it wants.
const clerk = vi.hoisted(() => ({
  auth: { isLoaded: true, isSignedIn: true, orgId: null as string | null },
  user: { isLoaded: true, user: null as unknown },
  organization: null as { name: string } | null,
  setActive: vi.fn(),
}))

vi.mock('@clerk/react', () => ({
  useAuth: () => ({
    ...clerk.auth,
    getToken: async () => 'clerk-token',
    signOut: async () => undefined,
  }),
  useUser: () => clerk.user,
  useOrganization: () => ({ organization: clerk.organization }),
  useClerk: () => ({ setActive: clerk.setActive }),
  // The separate list the bridge used to read: empty, as it is until its own fetch returns.
  useOrganizationList: () => ({ userMemberships: { data: [], isLoading: true } }),
  OrganizationSwitcher: () => null,
}))

const tahier = (memberships: { id: string; name: string }[]) => ({
  id: 'user_t',
  fullName: 'Tahier',
  primaryEmailAddress: { emailAddress: 'tahier@example.com' },
  organizationMemberships: memberships.map((o) => ({ organization: o, role: 'org:admin' })),
})

function renderConsole() {
  const router = createMemoryRouter(
    [
      {
        path: '/',
        element: <ConsoleGate />,
        children: [{ index: true, element: <p>Overview page</p> }],
      },
      { path: '/create-org', element: <p>Create organization</p> },
      { path: '/sign-in', element: <p>Sign in</p> },
    ],
    { initialEntries: ['/'] },
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ClerkBridge>
        <RouterProvider router={router} />
      </ClerkBridge>
    </QueryClientProvider>,
  )
  return router
}

const me = {
  user: { id: 'u', email: 'tahier@example.com', name: 'Tahier' },
  org: { id: 'o', name: "Tahier's Organization" },
  membership: { id: 'm', privilege: 'admin', roles: [] },
}

beforeEach(() => {
  clerk.auth = { isLoaded: true, isSignedIn: true, orgId: null }
  clerk.user = { isLoaded: true, user: null }
  clerk.organization = null
  vi.stubGlobal('fetch', mockFetch({ 'GET /api/me': () => ({ body: me }) }).fn)
})
afterEach(() => vi.unstubAllGlobals())

describe('Clerk sign in', () => {
  it('takes an admin with an org to the console, not to Create organization', async () => {
    clerk.auth.orgId = 'org_1'
    clerk.user = { isLoaded: true, user: tahier([{ id: 'org_1', name: "Tahier's Organization" }]) }
    const router = renderConsole()
    expect(await screen.findByText('Overview page')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/')
  })

  it('waits for Clerk to load the user instead of deciding there is no org', async () => {
    clerk.user = { isLoaded: false, user: null }
    const router = renderConsole()
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(router.state.location.pathname).toBe('/')
    expect(screen.queryByText('Create organization')).not.toBeInTheDocument()
  })

  it('counts an org just created before the memberships catch up', async () => {
    clerk.auth.orgId = 'org_new'
    clerk.organization = { name: 'New Org' }
    clerk.user = { isLoaded: true, user: tahier([]) }
    const router = renderConsole()
    expect(await screen.findByText('Overview page')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/')
  })

  it('sends someone with no org at all to Create organization', async () => {
    clerk.user = { isLoaded: true, user: tahier([]) }
    const router = renderConsole()
    expect(await screen.findByText('Create organization')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/create-org')
  })

  it('sends a signed out visitor to sign in', async () => {
    clerk.auth = { isLoaded: true, isSignedIn: false, orgId: null }
    const router = renderConsole()
    expect(await screen.findByText('Sign in')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/sign-in')
  })
})
