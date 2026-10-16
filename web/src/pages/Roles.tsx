import { useState, type FormEvent } from 'react'
import { json, useApiMutation, useRoles, type Role } from '../api/hooks'
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
  Spinner,
  TextArea,
} from '../components/ui'

function RoleForm({ role, onClose }: { role: Role | null; onClose: () => void }) {
  const [title, setTitle] = useState(role?.title ?? '')
  const [description, setDescription] = useState(role?.description ?? '')
  const save = useApiMutation(
    (api, body: { title: string; description: string }) =>
      role
        ? api(`/api/roles/${role.id}`, { method: 'PATCH', ...json(body) })
        : api('/api/roles', { method: 'POST', ...json(body) }),
    ['roles', 'members', 'org'],
  )
  const submit = (e: FormEvent) => {
    e.preventDefault()
    save.mutate({ title, description }, { onSuccess: onClose })
  }
  return (
    <form onSubmit={submit} className="space-y-4">
      <ErrorNotice error={save.error} />
      <Field label="Title" htmlFor="role-title">
        <Input
          id="role-title"
          required
          maxLength={80}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
      </Field>
      <Field
        label="How should the agent respond to people in this role?"
        htmlFor="role-description"
        hint="Tone, depth and format only. Roles never grant or restrict access."
      >
        <TextArea
          id="role-description"
          rows={4}
          maxLength={4000}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </Field>
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" busy={save.isPending}>
          {role ? 'Save role' : 'Create role'}
        </Button>
      </div>
    </form>
  )
}

export function RolesPage() {
  const me = useMember()
  const isAdmin = me.membership.privilege === 'admin'
  const [showArchived, setShowArchived] = useState(false)
  const roles = useRoles(showArchived)
  const [editing, setEditing] = useState<Role | 'new' | null>(null)
  const archive = useApiMutation(
    (api, id: string) => api(`/api/roles/${id}/archive`, { method: 'POST' }),
    ['roles', 'members', 'me', 'org'],
  )
  return (
    <>
      <PageHeader
        title="Roles"
        description="Custom roles tell the agent how to tailor its responses. A person can hold several; one is primary."
        actions={isAdmin ? <Button onClick={() => setEditing('new')}>New role</Button> : undefined}
      />
      <ErrorNotice error={archive.error} onDismiss={() => archive.reset()} />
      <Card
        actions={
          <label className="flex items-center gap-2 text-xs text-fg-2">
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(e) => setShowArchived(e.target.checked)}
            />
            Show archived
          </label>
        }
        title="All roles"
      >
        {roles.isPending ? (
          <Spinner />
        ) : roles.error ? (
          <ErrorNotice error={roles.error} />
        ) : roles.data.length === 0 ? (
          <EmptyState>No roles yet.</EmptyState>
        ) : (
          <ul className="divide-y divide-line">
            {roles.data.map((role) => (
              <li
                key={role.id}
                className="flex flex-wrap items-start justify-between gap-3 py-3"
                data-testid={`role-${role.title}`}
              >
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium">
                    {role.title} {role.status === 'archived' && <Badge>Archived</Badge>}
                  </p>
                  <p className="mt-0.5 text-sm text-fg-2">
                    {role.description || <span className="text-fg-4">No description</span>}
                  </p>
                  <p className="mt-1 text-xs text-fg-3">
                    {role.members} member{role.members === 1 ? '' : 's'} · primary for{' '}
                    {role.primary_for}
                  </p>
                </div>
                {isAdmin && role.status === 'active' && (
                  <div className="flex gap-1">
                    <Button variant="ghost" onClick={() => setEditing(role)}>
                      Edit
                    </Button>
                    <Button
                      variant="ghost"
                      className="text-bad"
                      busy={archive.isPending && archive.variables === role.id}
                      onClick={() => archive.mutate(role.id)}
                    >
                      Archive
                    </Button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Dialog
        open={editing !== null}
        title={editing === 'new' ? 'New role' : 'Edit role'}
        onClose={() => setEditing(null)}
      >
        {editing && (
          <RoleForm role={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />
        )}
      </Dialog>
    </>
  )
}
