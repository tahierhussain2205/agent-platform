import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ConsoleGate } from '../components/Layout'
import { mockFetch, renderRoutes } from '../test/render'
import { SlackPage } from './Slack'

const me = (privilege: string) => ({
  user: { id: 'u', email: 'a@acme.test', name: 'A' },
  org: { id: 'o', name: 'Acme' },
  membership: { id: 'm', privilege, roles: [] },
})
const routes = [
  { path: '/', element: <ConsoleGate />, children: [{ path: 'slack', element: <SlackPage /> }] },
]
const notConnected = { workspace: null, channels: [], install_configured: true }
const connected = {
  workspace: {
    id: 'w',
    external_id: 'T0ACME',
    name: 'Acme',
    bot_user_id: 'U0BOT',
    installed_at: '',
  },
  install_configured: true,
  channels: [
    {
      id: 'c1',
      external_id: 'C1',
      name: 'bugs',
      is_private: false,
      is_allowlisted: true,
      bot_is_member: true,
      archived: false,
      members: 3,
      schedules: 0,
    },
    {
      id: 'c2',
      external_id: 'C2',
      name: 'random',
      is_private: false,
      is_allowlisted: false,
      bot_is_member: false,
      archived: false,
      members: 4,
      schedules: 0,
    },
  ],
}

afterEach(() => vi.unstubAllGlobals())

describe('Slack page', () => {
  it('lets admins connect and tells members to ask an admin', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me('admin') }),
        'GET /api/slack': () => ({ body: notConnected }),
      }).fn,
    )
    const { unmount } = renderRoutes(routes, { path: '/slack' })
    expect(await screen.findByRole('button', { name: 'Connect Slack' })).toBeEnabled()
    unmount()
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me('user') }),
        'GET /api/slack': () => ({ body: notConnected }),
      }).fn,
    )
    renderRoutes(routes, { path: '/slack' })
    expect(
      await screen.findByText('Slack is not connected yet. Ask an admin to connect it.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Connect Slack' })).not.toBeInTheDocument()
  })

  it('explains install errors from the OAuth redirect', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me('admin') }),
        'GET /api/slack': () => ({ body: notConnected }),
      }).fn,
    )
    renderRoutes(routes, { path: '/slack?error=workspace_taken' })
    expect(
      await screen.findByText('That Slack workspace is already connected to another organization.'),
    ).toBeInTheDocument()
  })

  it('shows channels, bot membership hints and read only toggles for members', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me('user') }),
        'GET /api/slack': () => ({ body: connected }),
      }).fn,
    )
    renderRoutes(routes, { path: '/slack?installed=Acme' })
    expect(await screen.findByText(/Connected to the Acme Slack workspace/)).toBeInTheDocument()
    expect(screen.getByText('Agent not in channel: /invite @agent')).toBeInTheDocument()
    expect(screen.getByLabelText('Allow the agent in bugs')).toBeChecked()
    expect(screen.getByLabelText('Allow the agent in random')).toBeDisabled() // members cannot change it
    expect(screen.queryByRole('button', { name: 'Disconnect' })).not.toBeInTheDocument()
  })

  it('shows the new allowlist state while it saves, then the saved state', async () => {
    let allowRandom = false
    let release = () => {}
    const saved = new Promise<void>((resolve) => (release = resolve))
    const mock = mockFetch({
      'GET /api/me': () => ({ body: me('admin') }),
      'GET /api/slack': () => ({
        body: {
          ...connected,
          channels: connected.channels.map((c) =>
            c.id === 'c2' ? { ...c, is_allowlisted: allowRandom } : c,
          ),
        },
      }),
      'PATCH /api/slack/channels/c2': () => {
        allowRandom = true
        return { body: { paused_schedules: 0 } }
      },
    })
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'PATCH') await saved
      return mock.fn(input, init)
    })
    renderRoutes(routes, { path: '/slack' })
    const box = await screen.findByLabelText('Allow the agent in random')
    await userEvent.click(box)
    expect(box).toBeChecked() // before the server answered
    expect(box).toBeDisabled()
    release()
    await waitFor(() => expect(box).toBeEnabled())
    expect(box).toBeChecked()
    expect(JSON.parse(String(mock.calls.find((c) => c.method === 'PATCH')?.init?.body))).toEqual({
      is_allowlisted: true,
    })
  })
})
