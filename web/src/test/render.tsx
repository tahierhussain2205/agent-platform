import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import type { ReactElement } from 'react'
import { createMemoryRouter, RouterProvider, type RouteObject } from 'react-router'
import { AuthContext } from '../auth/context'
import type { AuthState } from '../auth/types'

export function fakeAuth(overrides: Partial<AuthState> = {}): AuthState {
  return {
    mode: 'dev',
    status: 'signed_in',
    user: { id: 'user_alice', email: 'alice@acme.test', name: 'Alice Admin' },
    orgs: [{ id: 'org_acme', name: 'Acme Corp', role: 'org:admin' }],
    activeOrgId: 'org_acme',
    invitations: [],
    getToken: async () => 'test-token',
    setActiveOrg: async () => undefined,
    signOut: async () => undefined,
    ...overrides,
  }
}

/** Renders routes in a memory router with a fake auth context and a fresh query client. */
export function renderRoutes(
  routes: RouteObject[],
  { auth = fakeAuth(), path = '/' }: { auth?: AuthState; path?: string } = {},
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  return {
    router,
    ...render(
      <AuthContext.Provider value={auth}>
        <QueryClientProvider client={client}>
          <RouterProvider router={router} />
        </QueryClientProvider>
      </AuthContext.Provider>,
    ),
  }
}

export function renderUi(ui: ReactElement, auth = fakeAuth()) {
  return renderRoutes([{ path: '/', element: ui }], { auth })
}

/** A fetch mock answering by "METHOD path" with JSON bodies. */
export function mockFetch(
  routes: Record<
    string,
    (init?: RequestInit) => { status?: number; body?: unknown; headers?: Record<string, string> }
  >,
) {
  const calls: { method: string; url: string; init?: RequestInit }[] = []
  const fn = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input.toString()
    const method = init?.method ?? 'GET'
    calls.push({ method, url, init })
    const path = new URL(url).pathname + new URL(url).search
    const handler = routes[`${method} ${path}`] ?? routes[`${method} ${new URL(url).pathname}`]
    if (!handler)
      return new Response(
        JSON.stringify({ error: { code: 'not_found', message: `unmocked ${method} ${path}` } }),
        { status: 404 },
      )
    const { status = 200, body, headers = {} } = handler(init)
    return new Response(status === 204 ? null : JSON.stringify(body ?? {}), {
      status,
      headers: { 'content-type': 'application/json', ...headers },
    })
  }
  return { fn, calls }
}
