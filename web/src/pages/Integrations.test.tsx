import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Integration } from '../api/hooks'
import { ConsoleGate } from '../components/Layout'
import { mockFetch, renderRoutes } from '../test/render'
import { IntegrationsPage } from './Integrations'

const me = (privilege: string) => ({
  user: { id: 'u', email: 'a@acme.test', name: 'A' },
  org: { id: 'o', name: 'Acme' },
  membership: { id: 'm', privilege, roles: [] },
})
const routes = [
  {
    path: '/',
    element: <ConsoleGate />,
    children: [{ path: 'integrations', element: <IntegrationsPage /> }],
  },
]

const connection = (
  account: string,
  status: 'active' | 'expired' = 'active',
  label: string | null = null,
) => ({
  id: `c-${account}`,
  status,
  external_account_id: account,
  external_account_label: label,
  created_at: '2026-10-01T00:00:00Z',
  expires_at: null,
  last_refreshed_at: null,
})

function integration(overrides: Partial<Integration>): Integration {
  return {
    key: 'jira',
    title: 'Jira',
    kind: 'native',
    auth: 'oauth2',
    description: 'Search and read issues.',
    user_links: true,
    enabled: false,
    health: 'unknown',
    approved: true,
    configured: true,
    connection: null,
    link: null,
    operations: [
      {
        name: 'search_issues',
        description: 'Search issues with JQL.',
        tier: 'read',
        default_mode: 'org_with_check',
        mode: 'org_with_check',
        enabled: true,
        overridden: false,
      },
    ],
    ...overrides,
  }
}

const slack = integration({
  key: 'slack',
  title: 'Slack',
  auth: 'slack',
  user_links: false,
  enabled: true,
  operations: [
    {
      name: 'read_channel',
      description: 'Read a channel.',
      tier: 'read',
      default_mode: 'org_with_check',
      mode: 'org_with_check',
      enabled: true,
      overridden: false,
    },
  ],
})

function stub(privilege: string, integrations: Integration[], extra = {}) {
  const mock = mockFetch({
    'GET /api/me': () => ({ body: me(privilege) }),
    'GET /api/integrations': () => ({ body: { integrations } }),
    ...extra,
  })
  vi.stubGlobal('fetch', mock.fn)
  return mock
}

afterEach(() => vi.unstubAllGlobals())

describe('Integrations page', () => {
  it('lets admins enable and connect, and points Slack to its own page', async () => {
    let enabled = false
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me('admin') }),
        'GET /api/integrations': () => ({
          body: { integrations: [slack, integration({ enabled })] },
        }),
        'PATCH /api/integrations/jira': () => {
          enabled = true
          return { body: { key: 'jira', enabled: true } }
        },
      }).fn,
    )
    renderRoutes(routes, { path: '/integrations' })
    const jira = await screen.findByRole('region', { name: 'Jira' })
    expect(within(jira).getByRole('button', { name: 'Connect Jira' })).toBeEnabled()
    expect(within(jira).getByText('not connected')).toBeInTheDocument()
    expect(within(jira).queryByRole('button', { name: 'Link my account' })).not.toBeInTheDocument()
    const slackCard = screen.getByRole('region', { name: 'Slack' })
    expect(within(slackCard).getByRole('link', { name: 'Managed on the Slack page' })).toBeVisible()
    expect(within(slackCard).queryByLabelText('Enable Slack')).not.toBeInTheDocument()
    expect(within(slackCard).getByText('Not connected')).toBeInTheDocument()
    await userEvent.click(within(slackCard).getByText(/^Operations/))
    const slackModes = within(slackCard).getByLabelText('Credential for read_channel')
    expect(
      within(slackModes).queryByRole('option', { name: /own account/ }),
    ).not.toBeInTheDocument()

    await userEvent.click(within(jira).getByLabelText('Enable Jira'))
    await waitFor(() => expect(within(jira).getByLabelText('Enable Jira')).toBeChecked())
    // Enabled integrations with links offer everyone to link their own account.
    expect(await within(jira).findByRole('button', { name: 'Link my account' })).toBeInTheDocument()
  })

  it('shows members the state read only, with their own link', async () => {
    stub('user', [
      integration({
        enabled: true,
        health: 'ok',
        connection: connection('acc-alice'),
        link: connection('acc-bob'),
      }),
    ])
    renderRoutes(routes, { path: '/integrations' })
    const jira = await screen.findByRole('region', { name: 'Jira' })
    expect(within(jira).getByTestId('jira-connection')).toHaveTextContent('connected as acc-alice')
    expect(within(jira).getByTestId('jira-link')).toHaveTextContent('linked as acc-bob')
    expect(within(jira).getByText('Healthy')).toBeInTheDocument()
    expect(within(jira).getByLabelText('Enable Jira')).toBeDisabled()
    expect(within(jira).queryByRole('button', { name: 'Disconnect' })).not.toBeInTheDocument()
    expect(within(jira).queryByRole('button', { name: 'Check now' })).not.toBeInTheDocument()
    expect(within(jira).getByRole('button', { name: 'Unlink' })).toBeEnabled()
    await userEvent.click(within(jira).getByText(/Operations \(1/))
    expect(within(jira).queryByLabelText('Credential for search_issues')).not.toBeInTheDocument()
    expect(within(jira).getByLabelText('Allow search_issues')).toBeDisabled()
  })

  it('names accounts by their label when there is one', async () => {
    stub('user', [
      integration({
        enabled: true,
        health: 'ok',
        connection: connection('f6b9594d-cloud', 'active', 'https://acme.atlassian.net'),
        link: connection('712020:acc-bob', 'expired', 'Bob Builder'),
      }),
    ])
    renderRoutes(routes, { path: '/integrations' })
    const jira = await screen.findByRole('region', { name: 'Jira' })
    expect(within(jira).getByTestId('jira-connection')).toHaveTextContent(
      'connected as acme.atlassian.net',
    )
    expect(within(jira).getByTestId('jira-link')).toHaveTextContent(
      'expired (Bob Builder), link it again',
    )
  })

  it('starts the OAuth flow with the URL the API returns', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    stub('user', [integration({ enabled: true })], {
      'GET /api/integrations/jira/link': () => ({ body: { url: 'http://localhost:4100/x' } }),
    })
    renderRoutes(routes, { path: '/integrations' })
    await userEvent.click(await screen.findByRole('button', { name: 'Link my account' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('http://localhost:4100/x'))
  })

  it('shows why a connect failed and dismisses it', async () => {
    stub('admin', [integration({})])
    renderRoutes(routes, { path: '/integrations?error=not_yours&integration=github' })
    expect(
      await screen.findByText('That installation does not belong to the account that approved it.'),
    ).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('confirms a successful link', async () => {
    stub('user', [integration({ enabled: true, link: connection('acc-bob') })])
    renderRoutes(routes, { path: '/integrations?linked=jira' })
    expect(await screen.findByText('Your Jira account is linked.')).toBeInTheDocument()
  })

  it('saves and resets a credential mode override', async () => {
    let override: { credential_mode: string; enabled: boolean } | null = null
    const mock = mockFetch({
      'GET /api/me': () => ({ body: me('admin') }),
      'GET /api/integrations': () => ({
        body: {
          integrations: [
            integration({
              enabled: true,
              connection: connection('acc-alice'),
              operations: [
                {
                  name: 'search_issues',
                  description: 'Search issues with JQL.',
                  tier: 'read',
                  default_mode: 'org_with_check',
                  mode: (override?.credential_mode ?? 'org_with_check') as 'org_with_check',
                  enabled: override?.enabled ?? true,
                  overridden: override !== null,
                },
              ],
            }),
          ],
        },
      }),
      'PUT /api/integrations/jira/operations/search_issues': (init) => {
        override = JSON.parse(String(init?.body)) as typeof override
        return { body: override }
      },
      'DELETE /api/integrations/jira/operations/search_issues': () => {
        override = null
        return { status: 204 }
      },
    })
    vi.stubGlobal('fetch', mock.fn)
    renderRoutes(routes, { path: '/integrations' })
    const jira = await screen.findByRole('region', { name: 'Jira' })
    await userEvent.click(within(jira).getByText(/Operations \(1\)/))
    const select = within(jira).getByLabelText('Credential for search_issues')
    expect(select).toHaveValue('org_with_check')
    expect(within(select).getAllByRole('option')).toHaveLength(3)
    await userEvent.selectOptions(select, 'user_required')
    await waitFor(() => expect(within(jira).getByText(/1 overridden/)).toBeInTheDocument())
    expect(override).toEqual({ credential_mode: 'user_required', enabled: true })
    expect(select).toHaveValue('user_required')

    await userEvent.click(within(jira).getByLabelText('Allow search_issues'))
    await waitFor(() =>
      expect(override).toEqual({ credential_mode: 'user_required', enabled: false }),
    )

    await userEvent.click(within(jira).getByRole('button', { name: 'Reset' }))
    await waitFor(() => expect(select).toHaveValue('org_with_check'))
    expect(within(jira).queryByRole('button', { name: 'Reset' })).not.toBeInTheDocument()
  })

  it('disconnects only after confirming', async () => {
    let connected = true
    const mock = mockFetch({
      'GET /api/me': () => ({ body: me('admin') }),
      'GET /api/integrations': () => ({
        body: {
          integrations: [
            integration({ enabled: true, connection: connected ? connection('acc-alice') : null }),
          ],
        },
      }),
      'DELETE /api/integrations/jira/connection': () => {
        connected = false
        return { status: 204 }
      },
    })
    vi.stubGlobal('fetch', mock.fn)
    renderRoutes(routes, { path: '/integrations' })
    const jira = await screen.findByRole('region', { name: 'Jira' })
    await userEvent.click(within(jira).getByRole('button', { name: 'Disconnect' }))
    expect(mock.calls.some((c) => c.method === 'DELETE')).toBe(false)
    const dialog = await screen.findByRole('dialog')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Disconnect' }))
    await waitFor(() =>
      expect(within(jira).getByTestId('jira-connection')).toHaveTextContent('not connected'),
    )
  })

  it('reports a health check result and its errors', async () => {
    stub('admin', [integration({ enabled: true, connection: connection('acc-alice') })], {
      'POST /api/integrations/jira/check': () => ({
        body: { key: 'jira', health: 'ok', detail: 'The connection works.' },
      }),
    })
    renderRoutes(routes, { path: '/integrations' })
    const jira = await screen.findByRole('region', { name: 'Jira' })
    await userEvent.click(within(jira).getByRole('button', { name: 'Check now' }))
    expect(await within(jira).findByText('The connection works.')).toBeInTheDocument()
  })

  it('adds a custom MCP server and approves it', async () => {
    let servers: Integration[] = []
    const mock = mockFetch({
      'GET /api/me': () => ({ body: me('admin') }),
      'GET /api/integrations': () => ({ body: { integrations: servers } }),
      'POST /api/integrations/custom-mcp': (init) => {
        const body = JSON.parse(String(init?.body)) as { title: string; url: string }
        servers = [
          integration({
            key: 'mcp_status',
            title: body.title,
            kind: 'custom_mcp',
            auth: 'api_key',
            user_links: false,
            approved: false,
            connection: connection('none'),
            operations: [],
          }),
        ]
        return { status: 201, body: { key: 'mcp_status', enabled: false, approved: false } }
      },
      'POST /api/integrations/mcp_status/approve': () => {
        servers = servers.map((s) => ({ ...s, approved: true, health: 'ok' as const }))
        return { body: { key: 'mcp_status', health: 'ok', detail: '2 tools available.' } }
      },
    })
    vi.stubGlobal('fetch', mock.fn)
    renderRoutes(routes, { path: '/integrations' })
    await userEvent.click(await screen.findByRole('button', { name: 'Add MCP server' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Status')
    await userEvent.type(within(dialog).getByLabelText('URL'), 'https://status.example/mcp')
    await userEvent.type(within(dialog).getByLabelText('API key'), 'secret-key')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Add server' }))
    const card = await screen.findByRole('region', { name: 'Status' })
    expect(within(card).getByText('Waiting for approval')).toBeInTheDocument()
    expect(within(card).queryByLabelText('Enable Status')).not.toBeInTheDocument()
    expect(within(card).getByTestId('mcp_status-connection')).toHaveTextContent('API key stored')
    const sent = JSON.parse(
      String(mock.calls.find((c) => c.method === 'POST')?.init?.body),
    ) as Record<string, string>
    expect(sent).toEqual({
      title: 'Status',
      url: 'https://status.example/mcp',
      api_key: 'secret-key',
    })

    await userEvent.click(within(card).getByRole('button', { name: 'Approve' }))
    expect(await within(card).findByLabelText('Enable Status')).not.toBeChecked()
    expect(within(card).queryByText('Waiting for approval')).not.toBeInTheDocument()
    expect(within(card).getByText('2 tools available.')).toBeInTheDocument()
  })

  it('shows an API error when enabling is refused', async () => {
    stub('admin', [integration({})], {
      'PATCH /api/integrations/jira': () => ({
        status: 409,
        body: { error: { code: 'approval_required', message: 'Approve it first.' } },
      }),
    })
    renderRoutes(routes, { path: '/integrations' })
    const jira = await screen.findByRole('region', { name: 'Jira' })
    await userEvent.click(within(jira).getByLabelText('Enable Jira'))
    expect(await within(jira).findByText('Approve it first.')).toBeInTheDocument()
    expect(within(jira).getByLabelText('Enable Jira')).not.toBeChecked()
  })

  it('connects Cloud Logging with a service account key file', async () => {
    let sent: unknown = null
    let connected = false
    const logs = () =>
      integration({
        key: 'google_cloud_logging',
        title: 'Google Cloud Logging',
        auth: 'service_account',
        user_links: false,
        enabled: connected,
        connection: connected ? connection('agent-reader@acme-prod.iam.gserviceaccount.com') : null,
      })
    const mock = mockFetch({
      'GET /api/me': () => ({ body: me('admin') }),
      'GET /api/integrations': () => ({ body: { integrations: [logs()] } }),
      'POST /api/integrations/google_cloud_logging/service-account': (init) => {
        sent = JSON.parse(String(init?.body))
        connected = true
        return {
          body: {
            key: 'google_cloud_logging',
            account: 'agent-reader@acme-prod.iam.gserviceaccount.com',
            project_id: 'acme-prod',
          },
        }
      },
    })
    vi.stubGlobal('fetch', mock.fn)
    renderRoutes(routes, { path: '/integrations' })
    const card = await screen.findByRole('region', { name: 'Google Cloud Logging' })
    expect(within(card).queryByRole('button', { name: /Connect Google/ })).not.toBeInTheDocument()
    const connect = within(card).getByRole('button', { name: 'Connect' })
    expect(connect).toBeDisabled()
    const key = '{"type": "service_account", "client_email": "x"}'
    await userEvent.upload(
      within(card).getByLabelText('Service account key file'),
      new File([key], 'key.json', { type: 'application/json' }),
    )
    await userEvent.click(connect)
    expect(
      await within(card).findByText(
        'Connected as agent-reader@acme-prod.iam.gserviceaccount.com (project acme-prod).',
      ),
    ).toBeInTheDocument()
    expect(sent).toEqual({ key_json: key })
    expect(await within(card).findByRole('button', { name: 'Replace key' })).toBeInTheDocument()
    expect(within(card).getByTestId('google_cloud_logging-connection')).toHaveTextContent(
      'connected as agent-reader@acme-prod.iam.gserviceaccount.com',
    )
  })

  it('does not offer members the key upload', async () => {
    stub('user', [
      integration({
        key: 'google_cloud_logging',
        title: 'Google Cloud Logging',
        auth: 'service_account',
        user_links: false,
      }),
    ])
    renderRoutes(routes, { path: '/integrations' })
    const card = await screen.findByRole('region', { name: 'Google Cloud Logging' })
    expect(within(card).queryByLabelText('Service account key file')).not.toBeInTheDocument()
  })
})
