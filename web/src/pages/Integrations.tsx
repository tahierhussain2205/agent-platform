import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'
import {
  json,
  useApi,
  useApiMutation,
  useIntegrations,
  type CredentialMode,
  type Integration,
  type IntegrationConnection,
  type IntegrationOperation,
} from '../api/hooks'
import { useMember } from '../components/member'
import {
  Badge,
  Button,
  Card,
  Dialog,
  ErrorNotice,
  Field,
  Input,
  PageHeader,
  Select,
  Spinner,
} from '../components/ui'

const RESULT_ERRORS: Record<string, string> = {
  access_denied: 'The connection was cancelled at the provider.',
  provider_error: 'The provider reported an error. Try again.',
  bad_state: 'The connect link was not valid. Try again.',
  expired_state: 'The connect link expired. Try again.',
  state_reused: 'That connect link was already used. Start again.',
  not_allowed: 'You are not allowed to make this connection.',
  not_yours: 'That installation does not belong to the account that approved it.',
  exchange_failed: 'The provider did not complete the connection. Try again.',
  not_configured: 'This integration is not configured for this environment.',
  github_install_cancelled: 'The GitHub App installation was cancelled.',
}

const MODE_LABELS: Record<CredentialMode, string> = {
  org: 'Org account',
  org_with_check: 'Org account, checked against the requester',
  user_required: "Requester's own account",
}

const TIER_LABELS: Record<IntegrationOperation['tier'], string> = {
  read: 'Read',
  reversible_write: 'Write',
  irreversible: 'Irreversible',
}

const HEALTH: Record<
  Integration['health'],
  { tone: 'slate' | 'green' | 'amber' | 'red'; label: string }
> = {
  unknown: { tone: 'slate', label: 'Health unknown' },
  ok: { tone: 'green', label: 'Healthy' },
  degraded: { tone: 'amber', label: 'Degraded' },
  broken: { tone: 'red', label: 'Broken' },
}

function ResultBanner({ titles }: { titles: Record<string, string> }) {
  const [params, setParams] = useSearchParams()
  const connected = params.get('connected')
  const linked = params.get('linked')
  const error = params.get('error')
  if (!connected && !linked && !error) return null
  const dismiss = () => setParams({}, { replace: true })
  const title = (key: string) => titles[key] ?? key
  return (
    <div className="mb-4">
      {connected || linked ? (
        <div
          role="status"
          className="flex justify-between rounded-md bg-green-50 p-3 text-sm text-green-800 ring-1 ring-green-200"
        >
          <span>
            {connected
              ? `${title(connected)} is connected.`
              : `Your ${title(linked ?? '')} account is linked.`}
          </span>
          <button type="button" onClick={dismiss} aria-label="Dismiss">
            ×
          </button>
        </div>
      ) : (
        <ErrorNotice
          error={new Error(RESULT_ERRORS[error ?? ''] ?? `The connection failed (${error}).`)}
          onDismiss={dismiss}
        />
      )}
    </div>
  )
}

function useRedirect(path: string) {
  const api = useApi()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const go = async () => {
    setBusy(true)
    setError(null)
    try {
      const { url } = await api<{ url: string }>(path)
      window.location.assign(url)
    } catch (e) {
      setError(e)
      setBusy(false)
    }
  }
  return { busy, error, go: () => void go(), reset: () => setError(null) }
}

function account(connection: IntegrationConnection) {
  return connection.external_account_id ?? 'an account'
}

/** Connect with a service account key file: read in the browser, sent once to the API, kept by
 * the vault. The page never shows the key again. */
function KeyUpload({ integration }: { integration: Integration }) {
  const { key, connection } = integration
  const [file, setFile] = useState<File | null>(null)
  const upload = useApiMutation<File, { account: string; project_id: string | null }>(
    async (api, chosen) =>
      api(`/api/integrations/${key}/service-account`, {
        method: 'POST',
        ...json({ key_json: await chosen.text() }),
      }),
    ['integrations'],
  )
  return (
    <form
      className="flex flex-wrap items-end gap-2"
      onSubmit={(e) => {
        e.preventDefault()
        if (file) upload.mutate(file, { onSuccess: () => setFile(null) })
      }}
    >
      <Field
        label="Service account key file"
        htmlFor={`${key}-key`}
        hint="A JSON key with read access to logs"
      >
        <Input
          id={`${key}-key`}
          type="file"
          accept="application/json,.json"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
      </Field>
      <Button type="submit" busy={upload.isPending} disabled={!file}>
        {connection ? 'Replace key' : 'Connect'}
      </Button>
      {upload.data && (
        <p className="w-full text-sm text-green-700">
          Connected as {upload.data.account}
          {upload.data.project_id ? ` (project ${upload.data.project_id})` : ''}.
        </p>
      )}
      <div className="w-full">
        <ErrorNotice error={upload.error} onDismiss={() => upload.reset()} />
      </div>
    </form>
  )
}

function OrgConnection({ integration, isAdmin }: { integration: Integration; isAdmin: boolean }) {
  const { key, title, connection } = integration
  const connect = useRedirect(`/api/integrations/${key}/connect`)
  const disconnect = useApiMutation(
    (api) => api(`/api/integrations/${key}/connection`, { method: 'DELETE' }),
    ['integrations'],
  )
  const [confirm, setConfirm] = useState(false)
  const oauth = integration.auth === 'oauth2' || integration.auth === 'github_app'
  return (
    <div className="space-y-2">
      <ErrorNotice
        error={connect.error ?? disconnect.error}
        onDismiss={() => {
          connect.reset()
          disconnect.reset()
        }}
      />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm" data-testid={`${key}-connection`}>
          <span className="font-medium">Org connection: </span>
          {connection ? (
            connection.status === 'expired' ? (
              <span className="text-amber-700">expired ({account(connection)}), reconnect it</span>
            ) : integration.auth === 'api_key' ? (
              'API key stored'
            ) : (
              <>connected as {account(connection)}</>
            )
          ) : isAdmin ? (
            <span className="text-slate-600">not connected</span>
          ) : (
            <span className="text-slate-600">not connected. Ask an admin to connect it.</span>
          )}
        </p>
        {isAdmin && (
          <div className="flex gap-2">
            {oauth && (
              <Button
                variant={connection ? 'secondary' : 'primary'}
                busy={connect.busy}
                disabled={!integration.configured}
                onClick={connect.go}
              >
                {connection ? 'Reconnect' : `Connect ${title}`}
              </Button>
            )}
            {connection && (
              <Button variant="danger" onClick={() => setConfirm(true)}>
                Disconnect
              </Button>
            )}
          </div>
        )}
      </div>
      {isAdmin && integration.auth === 'service_account' && <KeyUpload integration={integration} />}
      <Dialog open={confirm} title={`Disconnect ${title}`} onClose={() => setConfirm(false)}>
        <div className="space-y-4">
          <p className="text-sm text-slate-700">
            The org connection is revoked at the provider and its token deleted. Operations that use
            the org account stop working until it is connected again.
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setConfirm(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              busy={disconnect.isPending}
              onClick={() => disconnect.mutate(undefined, { onSettled: () => setConfirm(false) })}
            >
              Disconnect
            </Button>
          </div>
        </div>
      </Dialog>
    </div>
  )
}

function MyLink({ integration }: { integration: Integration }) {
  const { key, title, link } = integration
  const start = useRedirect(`/api/integrations/${key}/link`)
  const unlink = useApiMutation(
    (api) => api(`/api/integrations/${key}/link`, { method: 'DELETE' }),
    ['integrations'],
  )
  return (
    <div className="space-y-2">
      <ErrorNotice
        error={start.error ?? unlink.error}
        onDismiss={() => {
          start.reset()
          unlink.reset()
        }}
      />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm" data-testid={`${key}-link`}>
          <span className="font-medium">Your account: </span>
          {link ? (
            link.status === 'expired' ? (
              <span className="text-amber-700">expired ({account(link)}), link it again</span>
            ) : (
              <>linked as {account(link)}</>
            )
          ) : (
            <span className="text-slate-600">
              not linked. Some answers need your own {title} account.
            </span>
          )}
        </p>
        <div className="flex gap-2">
          <Button
            variant={link ? 'secondary' : 'primary'}
            busy={start.busy}
            disabled={!integration.configured}
            onClick={start.go}
          >
            {link ? 'Link again' : 'Link my account'}
          </Button>
          {link && (
            <Button
              variant="ghost"
              busy={unlink.isPending}
              onClick={() => unlink.mutate(undefined)}
            >
              Unlink
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}

function OperationRow({
  integration,
  op,
  isAdmin,
}: {
  integration: Integration
  op: IntegrationOperation
  isAdmin: boolean
}) {
  const path = `/api/integrations/${integration.key}/operations/${op.name}`
  const save = useApiMutation(
    (api, body: { credential_mode: CredentialMode; enabled: boolean }) =>
      api(path, { method: 'PUT', ...json(body) }),
    ['integrations'],
  )
  const reset = useApiMutation((api) => api(path, { method: 'DELETE' }), ['integrations'])
  // Keep the requested values while saving (the list refetches after the mutation).
  const [requested, setRequested] = useState<{ mode: CredentialMode; enabled: boolean } | null>(
    null,
  )
  const mode = requested?.mode ?? op.mode
  const enabled = requested?.enabled ?? op.enabled
  const change = (next: { mode: CredentialMode; enabled: boolean }) => {
    setRequested(next)
    save.mutate(
      { credential_mode: next.mode, enabled: next.enabled },
      { onSettled: () => setRequested(null) },
    )
  }
  return (
    <tr data-testid={`op-${integration.key}-${op.name}`}>
      <td className="py-2 pr-3 align-top text-sm">
        <div className="font-mono text-xs">{op.name}</div>
        <div className="text-xs text-slate-500">{op.description}</div>
        {(save.error ?? reset.error) && (
          <div className="mt-1">
            <ErrorNotice
              error={save.error ?? reset.error}
              onDismiss={() => {
                save.reset()
                reset.reset()
              }}
            />
          </div>
        )}
      </td>
      <td className="py-2 pr-3 align-top">
        <Badge tone={op.tier === 'read' ? 'slate' : op.tier === 'irreversible' ? 'red' : 'amber'}>
          {TIER_LABELS[op.tier]}
        </Badge>
      </td>
      <td className="py-2 pr-3 align-top text-sm">
        {isAdmin ? (
          <Select
            aria-label={`Credential for ${op.name}`}
            value={mode}
            disabled={requested !== null}
            onChange={(e) => change({ mode: e.target.value as CredentialMode, enabled })}
          >
            {(Object.keys(MODE_LABELS) as CredentialMode[])
              // Without account links, "the requester's own account" would only break the tool.
              .filter((m) => m !== 'user_required' || integration.user_links || m === mode)
              .map((m) => (
                <option key={m} value={m}>
                  {MODE_LABELS[m]}
                  {m === op.default_mode ? ' (default)' : ''}
                </option>
              ))}
          </Select>
        ) : (
          MODE_LABELS[mode]
        )}
      </td>
      <td className="py-2 pr-3 align-top">
        <label className="inline-flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            aria-label={`Allow ${op.name}`}
            checked={enabled}
            disabled={!isAdmin || requested !== null}
            onChange={(e) => change({ mode, enabled: e.target.checked })}
          />
          {enabled ? 'On' : 'Off'}
        </label>
      </td>
      <td className="py-2 text-right align-top">
        {isAdmin && op.overridden && (
          <Button variant="ghost" busy={reset.isPending} onClick={() => reset.mutate(undefined)}>
            Reset
          </Button>
        )}
      </td>
    </tr>
  )
}

function Operations({ integration, isAdmin }: { integration: Integration; isAdmin: boolean }) {
  const ops = integration.operations
  if (ops.length === 0)
    return (
      <p className="text-xs text-slate-500">
        {integration.kind === 'native'
          ? 'No operations.'
          : 'Operations appear after a successful health check lists the tools.'}
      </p>
    )
  const overridden = ops.filter((o) => o.overridden).length
  return (
    <details>
      <summary className="cursor-pointer text-sm font-medium text-slate-800">
        Operations ({ops.length}
        {overridden > 0 ? `, ${overridden} overridden` : ''})
      </summary>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-left">
          <thead className="text-xs uppercase text-slate-500">
            <tr>
              <th className="pb-2 pr-3 font-medium">Operation</th>
              <th className="pb-2 pr-3 font-medium">Kind</th>
              <th className="pb-2 pr-3 font-medium">Acts with</th>
              <th className="pb-2 pr-3 font-medium">Allowed</th>
              <th className="pb-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-200">
            {ops.map((op) => (
              <OperationRow key={op.name} integration={integration} op={op} isAdmin={isAdmin} />
            ))}
          </tbody>
        </table>
      </div>
    </details>
  )
}

function IntegrationCard({ integration, isAdmin }: { integration: Integration; isAdmin: boolean }) {
  const { key, title } = integration
  const toggle = useApiMutation(
    (api, enabled: boolean) =>
      api(`/api/integrations/${key}`, { method: 'PATCH', ...json({ enabled }) }),
    ['integrations'],
  )
  const check = useApiMutation(
    (api) =>
      api<{ health: string; detail: string }>(`/api/integrations/${key}/check`, { method: 'POST' }),
    ['integrations'],
  )
  const approve = useApiMutation(
    (api) =>
      api<{ health: string; detail: string }>(`/api/integrations/${key}/approve`, {
        method: 'POST',
      }),
    ['integrations'],
  )
  const [requested, setRequested] = useState<boolean | null>(null)
  const enabled = requested ?? integration.enabled
  const health = HEALTH[integration.health]
  const custom = integration.kind === 'custom_mcp'
  const slack = integration.auth === 'slack'
  const connectable = ['oauth2', 'github_app', 'service_account'].includes(integration.auth)
  const result = check.data ?? approve.data
  return (
    <section
      aria-label={title}
      data-testid={`integration-${key}`}
      className="rounded-lg bg-white shadow-sm ring-1 ring-slate-200"
    >
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
          {integration.kind !== 'native' && (
            <Badge tone="indigo">{custom ? 'Custom MCP' : 'MCP'}</Badge>
          )}
          {!integration.approved && <Badge tone="amber">Waiting for approval</Badge>}
          {slack ? (
            <Badge tone={integration.connection ? 'green' : 'slate'}>
              {integration.connection ? 'Connected' : 'Not connected'}
            </Badge>
          ) : (
            <Badge tone={health.tone}>{health.label}</Badge>
          )}
        </div>
        <div className="flex items-center gap-3">
          {slack ? (
            <Link to="/slack" className="text-sm text-indigo-600 hover:text-indigo-500">
              Managed on the Slack page
            </Link>
          ) : !integration.approved ? (
            isAdmin && (
              <Button busy={approve.isPending} onClick={() => approve.mutate(undefined)}>
                Approve
              </Button>
            )
          ) : (
            <label className="inline-flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                aria-label={`Enable ${title}`}
                checked={enabled}
                disabled={!isAdmin || requested !== null}
                onChange={(e) => {
                  setRequested(e.target.checked)
                  toggle.mutate(e.target.checked, { onSettled: () => setRequested(null) })
                }}
              />
              {enabled ? 'Enabled' : 'Disabled'}
            </label>
          )}
        </div>
      </header>
      <div className="space-y-3 p-4">
        <p className="text-sm text-slate-600">{integration.description}</p>
        <ErrorNotice
          error={toggle.error ?? check.error ?? approve.error}
          onDismiss={() => {
            toggle.reset()
            check.reset()
            approve.reset()
          }}
        />
        {!integration.configured && (
          <p className="text-sm text-amber-700">
            Not configured for this environment: its OAuth client is missing.
          </p>
        )}
        {!slack && (connectable || integration.connection) && (
          <OrgConnection integration={integration} isAdmin={isAdmin} />
        )}
        {integration.user_links && integration.enabled && <MyLink integration={integration} />}
        {isAdmin &&
          !slack &&
          integration.approved &&
          (integration.connection || integration.link) && (
            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="secondary"
                busy={check.isPending}
                onClick={() => check.mutate(undefined)}
              >
                Check now
              </Button>
              {result && (
                <p role="status" className="text-sm text-slate-600">
                  {result.detail}
                </p>
              )}
            </div>
          )}
        <Operations integration={integration} isAdmin={isAdmin} />
      </div>
    </section>
  )
}

function AddCustomMcp() {
  const [open, setOpen] = useState(false)
  const [title, setTitle] = useState('')
  const [url, setUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const add = useApiMutation(
    (api, body: { title: string; url: string; api_key: string | null }) =>
      api('/api/integrations/custom-mcp', { method: 'POST', ...json(body) }),
    ['integrations'],
  )
  const close = () => {
    setOpen(false)
    setTitle('')
    setUrl('')
    setApiKey('')
    add.reset()
  }
  const submit = (e: FormEvent) => {
    e.preventDefault()
    add.mutate({ title, url, api_key: apiKey || null }, { onSuccess: close })
  }
  return (
    <>
      <Button variant="secondary" onClick={() => setOpen(true)}>
        Add MCP server
      </Button>
      <Dialog open={open} title="Add an MCP server" onClose={close}>
        <form className="space-y-4" onSubmit={submit}>
          <p className="text-sm text-slate-600">
            The server is added disabled. An admin approves it before the agent may use it.
          </p>
          <ErrorNotice error={add.error} />
          <Field label="Name" htmlFor="mcp-title">
            <Input
              id="mcp-title"
              required
              maxLength={60}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </Field>
          <Field
            label="URL"
            htmlFor="mcp-url"
            hint="The server's Streamable HTTP endpoint (https)."
          >
            <Input
              id="mcp-url"
              required
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
          </Field>
          <Field
            label="API key"
            htmlFor="mcp-key"
            hint="Optional. Stored in the token vault, sent as a bearer token."
          >
            <Input
              id="mcp-key"
              type="password"
              autoComplete="off"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </Field>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button type="submit" busy={add.isPending}>
              Add server
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  )
}

export function IntegrationsPage() {
  const me = useMember()
  const isAdmin = me.membership.privilege === 'admin'
  const list = useIntegrations()
  if (list.isPending) return <Spinner />
  if (list.error) return <ErrorNotice error={list.error} />
  const integrations = list.data.integrations
  const titles = Object.fromEntries(integrations.map((i) => [i.key, i.title]))
  return (
    <>
      <PageHeader
        title="Integrations"
        description="Tools the agent may use. Each operation acts with the org account or the requester's own account, and never shows anyone what they could not see themselves."
        actions={isAdmin ? <AddCustomMcp /> : undefined}
      />
      <ResultBanner titles={titles} />
      <div className="space-y-4">
        {integrations.length === 0 ? (
          <Card>
            <p className="text-sm text-slate-600">No integrations available.</p>
          </Card>
        ) : (
          integrations.map((integration) => (
            <IntegrationCard key={integration.key} integration={integration} isAdmin={isAdmin} />
          ))
        )}
      </div>
    </>
  )
}
