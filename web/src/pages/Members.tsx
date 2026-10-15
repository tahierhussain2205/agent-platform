import { useState, type FormEvent } from 'react'
import {
  json,
  useApiMutation,
  useMembers,
  useRoles,
  type Member,
  type Privilege,
} from '../api/hooks'
import { useMember } from '../components/member'
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  ErrorNotice,
  Field,
  Input,
  PageHeader,
  Select,
  Spinner,
} from '../components/ui'

function InviteForm() {
  const [email, setEmail] = useState('')
  const [privilege, setPrivilege] = useState<Privilege>('user')
  const [sent, setSent] = useState<string | null>(null)
  const invite = useApiMutation(
    (api, body: { email: string; privilege: Privilege }) =>
      api('/api/members/invitations', { method: 'POST', ...json(body) }),
    ['members', 'org'],
  )
  const submit = (e: FormEvent) => {
    e.preventDefault()
    setSent(null)
    invite.mutate(
      { email, privilege },
      {
        onSuccess: () => {
          setSent(email)
          setEmail('')
        },
      },
    )
  }
  return (
    <form onSubmit={submit} className="space-y-3" aria-label="Invite a member">
      <ErrorNotice error={invite.error} />
      {sent && (
        <p role="status" className="rounded-md bg-green-50 p-2 text-sm text-green-800">
          Invitation sent to {sent}.
        </p>
      )}
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-56 flex-1">
          <Field label="Email" htmlFor="invite-email">
            <Input
              id="invite-email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="name@company.com"
            />
          </Field>
        </div>
        <div className="w-36">
          <Field label="Access" htmlFor="invite-privilege">
            <Select
              id="invite-privilege"
              value={privilege}
              onChange={(e) => setPrivilege(e.target.value as Privilege)}
            >
              <option value="user">Member</option>
              <option value="admin">Admin</option>
            </Select>
          </Field>
        </div>
        <Button type="submit" busy={invite.isPending}>
          Send invitation
        </Button>
      </div>
    </form>
  )
}

function RoleEditor({ member, onClose }: { member: Member; onClose: () => void }) {
  const roles = useRoles()
  const [selected, setSelected] = useState<string[]>(member.roles.map((r) => r.id))
  const [primary, setPrimary] = useState<string | null>(
    member.roles.find((r) => r.primary)?.id ?? null,
  )
  const save = useApiMutation(
    (api, body: { role_ids: string[]; primary_role_id: string | null }) =>
      api(`/api/members/${member.id}/roles`, { method: 'PUT', ...json(body) }),
    ['members', 'me', 'roles'],
  )
  const toggle = (id: string) => {
    const next = selected.includes(id) ? selected.filter((r) => r !== id) : [...selected, id]
    setSelected(next)
    if (!next.includes(primary ?? '')) setPrimary(next[0] ?? null)
    else if (primary === null && next.length) setPrimary(next[0]!)
  }
  if (roles.isPending) return <Spinner />
  return (
    <div className="space-y-4">
      <ErrorNotice error={save.error ?? roles.error} />
      {roles.data && roles.data.length === 0 ? (
        <EmptyState>No roles yet. Create roles on the Roles page.</EmptyState>
      ) : (
        <fieldset>
          <legend className="text-sm text-slate-600">
            Roles shape how the agent responds to {member.name ?? member.email}. Exactly one is
            primary.
          </legend>
          <ul className="mt-3 divide-y divide-slate-200 rounded-md ring-1 ring-slate-200">
            {roles.data?.map((role) => (
              <li key={role.id} className="flex items-center justify-between px-3 py-2">
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={selected.includes(role.id)}
                    onChange={() => toggle(role.id)}
                  />
                  {role.title}
                </label>
                <label className="flex items-center gap-1 text-xs text-slate-600">
                  <input
                    type="radio"
                    name="primary"
                    disabled={!selected.includes(role.id)}
                    checked={primary === role.id}
                    onChange={() => setPrimary(role.id)}
                    aria-label={`Primary: ${role.title}`}
                  />
                  Primary
                </label>
              </li>
            ))}
          </ul>
        </fieldset>
      )}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button
          busy={save.isPending}
          onClick={() =>
            save.mutate(
              { role_ids: selected, primary_role_id: selected.length ? primary : null },
              { onSuccess: onClose },
            )
          }
        >
          Save roles
        </Button>
      </div>
    </div>
  )
}

function ConfirmRemove({ member, onClose }: { member: Member; onClose: () => void }) {
  const remove = useApiMutation(
    (api, id: string) => api(`/api/members/${id}`, { method: 'DELETE' }),
    ['members', 'org'],
  )
  return (
    <div className="space-y-4">
      <ErrorNotice error={remove.error} />
      <p className="text-sm text-slate-700">
        Remove <strong>{member.name ?? member.email}</strong> from this organization? Their linked
        accounts are revoked, schedules they own are paused and their sessions are closed.
      </p>
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button
          variant="danger"
          busy={remove.isPending}
          onClick={() => remove.mutate(member.id, { onSuccess: onClose })}
        >
          Remove member
        </Button>
      </div>
    </div>
  )
}

function MemberRow({
  member,
  isAdmin,
  isSelf,
  onEditRoles,
  onRemove,
}: {
  member: Member
  isAdmin: boolean
  isSelf: boolean
  onEditRoles: () => void
  onRemove: () => void
}) {
  const change = useApiMutation(
    (api, privilege: Privilege) =>
      api(`/api/members/${member.id}`, { method: 'PATCH', ...json({ privilege }) }),
    ['members', 'org', 'me'],
  )
  const revoke = useApiMutation(
    (api, id: string) => api(`/api/members/invitations/${id}`, { method: 'DELETE' }),
    ['members', 'org'],
  )
  const error = change.error ?? revoke.error
  return (
    <tr className="align-top" data-testid={`member-${member.email}`}>
      <td className="py-3 pr-3">
        <p className="text-sm font-medium">
          {member.name ?? member.email}
          {isSelf && <span className="ml-1 text-xs text-slate-500">(you)</span>}
        </p>
        <p className="text-xs text-slate-500">{member.email}</p>
        {error && (
          <div className="mt-2">
            <ErrorNotice error={error} onDismiss={() => (change.reset(), revoke.reset())} />
          </div>
        )}
      </td>
      <td className="py-3 pr-3">
        {member.status === 'invited' ? (
          <Badge tone="amber">Invited</Badge>
        ) : (
          <Badge tone="green">Active</Badge>
        )}
        {member.slack_linked && (
          <span className="ml-1">
            <Badge>Slack</Badge>
          </span>
        )}
      </td>
      <td className="py-3 pr-3">
        <Badge tone={member.privilege === 'admin' ? 'indigo' : 'slate'}>
          {member.privilege === 'admin' ? 'Admin' : 'Member'}
        </Badge>
      </td>
      <td className="py-3 pr-3">
        <div className="flex flex-wrap gap-1">
          {member.roles.length === 0 && <span className="text-xs text-slate-400">None</span>}
          {member.roles.map((role) => (
            <Badge key={role.id} tone={role.primary ? 'indigo' : 'slate'}>
              {role.title}
              {role.primary ? ' ★' : ''}
            </Badge>
          ))}
        </div>
      </td>
      {isAdmin && (
        <td className="py-3 text-right">
          <div className="flex flex-wrap justify-end gap-1">
            {member.status === 'active' ? (
              <>
                <Button
                  variant="ghost"
                  busy={change.isPending}
                  onClick={() => change.mutate(member.privilege === 'admin' ? 'user' : 'admin')}
                >
                  {member.privilege === 'admin' ? 'Make member' : 'Make admin'}
                </Button>
                <Button variant="ghost" onClick={onEditRoles}>
                  Edit roles
                </Button>
                <Button variant="ghost" className="text-red-600" onClick={onRemove}>
                  Remove
                </Button>
              </>
            ) : (
              <Button
                variant="ghost"
                className="text-red-600"
                busy={revoke.isPending}
                onClick={() => revoke.mutate(member.id)}
              >
                Revoke invitation
              </Button>
            )}
          </div>
        </td>
      )}
    </tr>
  )
}

export function MembersPage() {
  const me = useMember()
  const members = useMembers()
  const [editing, setEditing] = useState<Member | null>(null)
  const [removing, setRemoving] = useState<Member | null>(null)
  const isAdmin = me.membership.privilege === 'admin'
  return (
    <>
      <PageHeader
        title="Members"
        description="People who can use the agent in Slack and this console."
      />
      {isAdmin && (
        <div className="mb-6">
          <Card title="Invite someone">
            <InviteForm />
          </Card>
        </div>
      )}
      <Card>
        {members.isPending ? (
          <Spinner />
        ) : members.error ? (
          <ErrorNotice error={members.error} />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead className="text-xs uppercase text-slate-500">
                <tr>
                  <th className="pb-2 pr-3 font-medium">Person</th>
                  <th className="pb-2 pr-3 font-medium">Status</th>
                  <th className="pb-2 pr-3 font-medium">Access</th>
                  <th className="pb-2 pr-3 font-medium">Roles</th>
                  {isAdmin && <th className="pb-2 font-medium sr-only">Actions</th>}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200">
                {members.data.map((member) => (
                  <MemberRow
                    key={member.id}
                    member={member}
                    isAdmin={isAdmin}
                    isSelf={member.id === me.membership.id}
                    onEditRoles={() => setEditing(member)}
                    onRemove={() => setRemoving(member)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <Dialog
        open={editing !== null}
        title={`Roles for ${editing?.name ?? editing?.email ?? ''}`}
        onClose={() => setEditing(null)}
      >
        {editing && <RoleEditor member={editing} onClose={() => setEditing(null)} />}
      </Dialog>
      <Dialog open={removing !== null} title="Remove member" onClose={() => setRemoving(null)}>
        {removing && <ConfirmRemove member={removing} onClose={() => setRemoving(null)} />}
      </Dialog>
    </>
  )
}
