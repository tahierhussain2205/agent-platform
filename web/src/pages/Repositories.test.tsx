import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Repository } from '../api/hooks'
import { ConsoleGate } from '../components/Layout'
import { mockFetch, renderRoutes } from '../test/render'
import { RepositoriesPage } from './Repositories'

const me = (privilege: string) => ({
  user: { id: 'u', email: 'alice@acme.test', name: 'Alice' },
  org: { id: 'o', name: 'Acme' },
  membership: { id: 'm-alice', privilege, roles: [] },
})
const routes = [
  {
    path: '/',
    element: <ConsoleGate />,
    children: [{ path: 'repositories', element: <RepositoriesPage /> }],
  },
]

function repository(overrides: Partial<Repository> = {}): Repository {
  return {
    id: 'r-cart',
    full_name: 'acme/cart-service',
    default_branch: 'main',
    status: 'active',
    protected_paths: ['migrations/'],
    recipe: {
      test: 'python -m pytest -q -p no:cacheprovider',
      source: 'detected',
      status: 'works',
      last_run: { at: '2026-10-16T10:00:00Z', exit_code: 0, summary: '3 passed in 0.12s' },
    },
    services: ['cart-service'],
    ...overrides,
  }
}

function stub(privilege: string, repos: () => Repository[], extra = {}) {
  const mock = mockFetch({
    'GET /api/me': () => ({ body: me(privilege) }),
    'GET /api/repositories': () => ({
      body: { repositories: repos(), can_manage: privilege === 'admin' },
    }),
    ...extra,
  })
  vi.stubGlobal('fetch', mock.fn)
  return mock
}

afterEach(() => vi.unstubAllGlobals())

describe('Repositories page', () => {
  it('shows members the protected paths and how the tests run, without controls', async () => {
    stub('user', () => [
      repository(),
      repository({
        id: 'r-web',
        full_name: 'acme/web-app',
        protected_paths: [],
        services: [],
        recipe: { test: null, source: null, status: null, last_run: null },
      }),
    ])
    renderRoutes(routes, { path: '/repositories' })
    const cart = await screen.findByRole('listitem', { name: 'acme/cart-service' })
    expect(within(cart).getByText('migrations/')).toBeInTheDocument()
    expect(within(cart).getByText('python -m pytest -q -p no:cacheprovider')).toBeInTheDocument()
    expect(within(cart).getByText('Tests ran')).toBeInTheDocument()
    expect(within(cart).getByText('Detected')).toBeInTheDocument()
    expect(within(cart).getByText(/3 passed in 0.12s/)).toBeInTheDocument()
    expect(within(cart).queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    const web = screen.getByRole('listitem', { name: 'acme/web-app' })
    expect(within(web).getByText(/CI workflows are always protected/)).toBeInTheDocument()
    expect(within(web).getByText(/detected from the repository the first time/)).toBeInTheDocument()
  })

  it('lets admins set protected paths and a test command', async () => {
    let current = repository()
    const mock = stub('admin', () => [current], {
      'PATCH /api/repositories/r-cart': (init?: RequestInit) => {
        const body = JSON.parse(String(init?.body)) as {
          protected_paths: string[]
          test_command: string
        }
        current = repository({
          protected_paths: body.protected_paths,
          recipe: { test: body.test_command, source: 'admin', status: 'untested', last_run: null },
        })
        return { body: current }
      },
    })
    renderRoutes(routes, { path: '/repositories' })
    const row = await screen.findByRole('listitem', { name: 'acme/cart-service' })
    await userEvent.click(within(row).getByRole('button', { name: 'Edit' }))
    const paths = screen.getByLabelText('Protected paths')
    await userEvent.clear(paths)
    await userEvent.type(paths, 'migrations/{enter}*.tf')
    await userEvent.type(screen.getByLabelText('Test command'), 'make test')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByText('*.tf')).toBeInTheDocument())
    const saved = mock.calls.find((c) => c.method === 'PATCH')
    expect(JSON.parse(String(saved?.init?.body))).toEqual({
      protected_paths: ['migrations/', '*.tf'],
      test_command: 'make test',
    })
    expect(screen.getByText('Set by an admin')).toBeInTheDocument()
    expect(screen.getByText('Not run yet')).toBeInTheDocument()
  })
})
