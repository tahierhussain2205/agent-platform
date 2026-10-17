import { useState } from 'react'
import { useSearchParams } from 'react-router'
import { json, useApi, useApiMutation, useSlack, type SlackChannel } from '../api/hooks'
import { useMember } from '../components/member'
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  ErrorNotice,
  PageHeader,
  Spinner,
} from '../components/ui'

const INSTALL_ERRORS: Record<string, string> = {
  access_denied: 'The installation was cancelled in Slack.',
  bad_state: 'The install link was not valid. Try again.',
  expired_state: 'The install link expired. Try again.',
  state_reused: 'That install link was already used. Start again.',
  exchange_failed: 'Slack did not complete the installation. Try again.',
  other_workspace:
    'This organization is already connected to another Slack workspace. Disconnect it first.',
  workspace_taken: 'That Slack workspace is already connected to another organization.',
  not_admin: 'Only admins can connect Slack.',
  storage_failed: 'The connection could not be saved. Try again.',
}

function InstallBanner() {
  const [params, setParams] = useSearchParams()
  const installed = params.get('installed')
  const error = params.get('error')
  if (!installed && !error) return null
  const dismiss = () => setParams({}, { replace: true })
  return (
    <div className="mb-4">
      {installed ? (
        <div
          role="status"
          className="flex justify-between rounded-md bg-ok-soft p-3 text-sm text-ok ring-1 ring-ok-line"
        >
          <span>Connected to the {installed} Slack workspace. Channels are being synced.</span>
          <button type="button" onClick={dismiss} aria-label="Dismiss">
            ×
          </button>
        </div>
      ) : (
        <ErrorNotice
          error={new Error(INSTALL_ERRORS[error ?? ''] ?? `Slack installation failed (${error}).`)}
          onDismiss={dismiss}
        />
      )}
    </div>
  )
}

function ChannelRow({ channel, isAdmin }: { channel: SlackChannel; isAdmin: boolean }) {
  const toggle = useApiMutation(
    (api, value: boolean) =>
      api<{ paused_schedules: number }>(`/api/slack/channels/${channel.id}`, {
        method: 'PATCH',
        ...json({ is_allowlisted: value }),
      }),
    ['slack', 'org', 'schedules'], // removing a channel pauses the schedules delivering there
  )
  const paused = toggle.data?.paused_schedules ?? 0
  // Show the requested state while saving, set in the same render as the click (the mutation only
  // reports pending a tick later); it is cleared once the list has refetched.
  const [requested, setRequested] = useState<boolean | null>(null)
  const allowed = requested ?? channel.is_allowlisted
  return (
    <tr data-testid={`channel-${channel.name}`}>
      <td className="whitespace-nowrap py-2 pr-3 text-sm font-medium">
        {channel.is_private ? '🔒 ' : '# '}
        {channel.name ?? channel.external_id}
        {toggle.error && (
          <div className="mt-1">
            <ErrorNotice error={toggle.error} onDismiss={() => toggle.reset()} />
          </div>
        )}
        {paused > 0 && (
          <p className="mt-1 text-xs text-warn">
            {paused} schedule(s) delivering here were paused.
          </p>
        )}
      </td>
      <td className="py-2 pr-3">
        <div className="flex flex-wrap gap-1">
          {channel.archived && <Badge>Archived</Badge>}
          {channel.bot_is_member ? (
            <Badge tone="green">Agent is in the channel</Badge>
          ) : (
            <span title="Slack only lets the agent read channels it belongs to">
              <Badge tone="amber">Agent not in channel: /invite @agent</Badge>
            </span>
          )}
        </div>
      </td>
      <td className="py-2 pr-3 text-sm text-fg-2">{channel.members}</td>
      <td className="py-2 text-right">
        <label className="inline-flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            aria-label={`Allow the agent in ${channel.name}`}
            checked={allowed}
            disabled={
              !isAdmin || requested !== null || (channel.archived && !channel.is_allowlisted)
            }
            onChange={(e) => {
              setRequested(e.target.checked)
              toggle.mutate(e.target.checked, { onSettled: () => setRequested(null) })
            }}
          />
          {allowed ? 'Allowed' : 'Not allowed'}
        </label>
      </td>
    </tr>
  )
}

export function SlackPage() {
  const me = useMember()
  const isAdmin = me.membership.privilege === 'admin'
  const slack = useSlack()
  const api = useApi()
  const [connecting, setConnecting] = useState(false)
  const [connectError, setConnectError] = useState<unknown>(null)
  const [confirm, setConfirm] = useState(false)
  const sync = useApiMutation((call) => call('/api/slack/sync', { method: 'POST' }), ['slack'])
  const disconnect = useApiMutation(
    (call) => call('/api/slack', { method: 'DELETE' }),
    ['slack', 'org'],
  )

  const connect = async () => {
    setConnecting(true)
    setConnectError(null)
    try {
      const { url } = await api<{ url: string }>('/api/slack/install')
      window.location.assign(url)
    } catch (e) {
      setConnectError(e)
      setConnecting(false)
    }
  }

  if (slack.isPending) return <Spinner />
  if (slack.error) return <ErrorNotice error={slack.error} />
  const { workspace, channels } = slack.data
  return (
    <>
      <PageHeader
        title="Slack"
        description="The agent reads a channel only if it is allowed here and the person asking is a member of it."
      />
      <InstallBanner />
      <div className="space-y-6">
        <Card
          title="Workspace"
          actions={
            isAdmin && workspace ? (
              <div className="flex gap-2">
                <Button
                  variant="secondary"
                  busy={sync.isPending}
                  onClick={() => sync.mutate(undefined)}
                >
                  Sync now
                </Button>
                <Button variant="danger" onClick={() => setConfirm(true)}>
                  Disconnect
                </Button>
              </div>
            ) : undefined
          }
        >
          <ErrorNotice error={connectError ?? sync.error ?? disconnect.error} />
          {sync.isSuccess && (
            <p role="status" className="mb-2 text-sm text-ok">
              Sync started. Channels and members refresh in a few seconds.
            </p>
          )}
          {workspace ? (
            <p className="text-sm">
              Connected to <strong>{workspace.name}</strong>{' '}
              <span className="text-fg-3">({workspace.external_id})</span>
            </p>
          ) : isAdmin ? (
            <div className="flex items-center justify-between gap-4">
              <p className="text-sm text-fg-2">Slack is not connected yet.</p>
              <Button
                busy={connecting}
                disabled={!slack.data.install_configured}
                onClick={() => void connect()}
              >
                Connect Slack
              </Button>
            </div>
          ) : (
            <p className="text-sm text-fg-2">
              Slack is not connected yet. Ask an admin to connect it.
            </p>
          )}
        </Card>
        {workspace && (
          <Card title="Channels">
            {channels.length === 0 ? (
              <EmptyState>No channels yet. Use Sync now after the first install.</EmptyState>
            ) : (
              <div className="relative overflow-x-auto">
                <table className="w-full text-left">
                  <thead className="text-xs uppercase text-fg-3">
                    <tr>
                      <th className="pb-2 pr-3 font-medium">Channel</th>
                      <th className="pb-2 pr-3 font-medium">Status</th>
                      <th className="pb-2 pr-3 font-medium">Members</th>
                      <th className="pb-2 text-right font-medium">Agent access</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line">
                    {channels.map((channel) => (
                      <ChannelRow key={channel.id} channel={channel} isAdmin={isAdmin} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        )}
      </div>
      <Dialog open={confirm} title="Disconnect Slack" onClose={() => setConfirm(false)}>
        <div className="space-y-4">
          <p className="text-sm text-fg-2">
            The agent stops working in Slack and its token is deleted. You can connect again later.
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setConfirm(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              busy={disconnect.isPending}
              onClick={() => disconnect.mutate(undefined, { onSuccess: () => setConfirm(false) })}
            >
              Disconnect
            </Button>
          </div>
        </div>
      </Dialog>
    </>
  )
}
