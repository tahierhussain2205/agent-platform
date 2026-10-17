import { useState, type FormEvent } from 'react'
import { json, useApiMutation, useOrgOverview } from '../api/hooks'
import { useMember } from '../components/member'
import {
  Badge,
  Button,
  Card,
  ErrorNotice,
  Field,
  Input,
  PageHeader,
  Spinner,
} from '../components/ui'

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-lg bg-surface p-4 shadow-sm ring-1 ring-line">
      <dt className="text-sm text-fg-3">{label}</dt>
      <dd
        className="mt-1 text-2xl font-semibold tracking-tight"
        data-testid={`stat-${label.toLowerCase().replace(/\s+/g, '-')}`}
      >
        {value}
      </dd>
    </div>
  )
}

function RenameOrg({ current, onDone }: { current: string; onDone: () => void }) {
  const [name, setName] = useState(current)
  const rename = useApiMutation(
    (api, value: string) => api('/api/org', { method: 'PATCH', ...json({ name: value }) }),
    ['org', 'me'],
  )
  const submit = (e: FormEvent) => {
    e.preventDefault()
    rename.mutate(name, { onSuccess: onDone })
  }
  return (
    <form onSubmit={submit} className="space-y-3">
      <ErrorNotice error={rename.error} />
      <Field label="Organization name" htmlFor="org-name">
        <Input
          id="org-name"
          value={name}
          maxLength={200}
          onChange={(e) => setName(e.target.value)}
        />
      </Field>
      <div className="flex gap-2">
        <Button type="submit" busy={rename.isPending}>
          Save
        </Button>
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  )
}

export function OverviewPage() {
  const me = useMember()
  const overview = useOrgOverview()
  const [renaming, setRenaming] = useState(false)
  if (overview.isPending) return <Spinner />
  if (overview.error) return <ErrorNotice error={overview.error} />
  const { org, counts, workspace } = overview.data
  const isAdmin = me.membership.privilege === 'admin'
  return (
    <>
      <PageHeader
        title={org.name}
        description={`Signed in as ${me.user.email}`}
        actions={
          isAdmin && !renaming ? (
            <Button variant="secondary" onClick={() => setRenaming(true)}>
              Rename
            </Button>
          ) : undefined
        }
      />
      {renaming && (
        <div className="mb-6 max-w-md">
          <Card title="Rename organization">
            <RenameOrg current={org.name} onDone={() => setRenaming(false)} />
          </Card>
        </div>
      )}
      <dl className="grid grid-cols-2 gap-4 lg:grid-cols-3">
        <Stat label="Members" value={counts.members} />
        <Stat label="Admins" value={counts.admins} />
        <Stat label="Pending invitations" value={counts.invited} />
        <Stat label="Roles" value={counts.roles} />
        <Stat label="Allowlisted channels" value={counts.allowlisted_channels} />
        <Stat label="Active schedules" value={counts.active_schedules} />
      </dl>
      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        <Card title="Slack workspace">
          {workspace ? (
            <p className="text-sm">
              {workspace.name} <span className="text-fg-3">({workspace.external_id})</span>{' '}
              <Badge tone="green">{workspace.status}</Badge>
            </p>
          ) : (
            <p className="text-sm text-fg-2">Not connected yet.</p>
          )}
        </Card>
        <Card title="Your roles">
          {me.membership.roles.length === 0 ? (
            <p className="text-sm text-fg-2">No roles assigned.</p>
          ) : (
            <ul className="flex flex-wrap gap-2">
              {me.membership.roles.map((role) => (
                <li key={role.id}>
                  <Badge tone={role.is_primary ? 'strong' : 'neutral'}>
                    {role.title}
                    {role.is_primary ? ' (primary)' : ''}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  )
}
