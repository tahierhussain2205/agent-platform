import { useState } from 'react'
import { useApiMutation, useRoleOptions, useRoles, type Role } from '../api/hooks'
import { useMember } from '../components/member'
import { RoleForm } from '../components/RoleEditor'
import { limitLabels, profileLabels } from '../components/roleProfile'
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

export function RolesPage() {
  const me = useMember()
  const isAdmin = me.membership.privilege === 'admin'
  const [showArchived, setShowArchived] = useState(false)
  const roles = useRoles(showArchived)
  const options = useRoleOptions()
  const [editing, setEditing] = useState<Role | 'new' | null>(null)
  const archive = useApiMutation(
    (api, id: string) => api(`/api/roles/${id}/archive`, { method: 'POST' }),
    ['roles', 'members', 'me', 'org'],
  )
  return (
    <>
      <PageHeader
        title="Roles"
        description="Roles tell the agent how to answer the people who hold them, and can narrow what it does for them. A person can hold several; one is primary."
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
                  {profileLabels(role.profile).length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {profileLabels(role.profile).map((label) => (
                        <Badge key={label}>{label}</Badge>
                      ))}
                    </div>
                  )}
                  {limitLabels(role, options.data).length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {limitLabels(role, options.data).map((label) => (
                        <Badge key={label} tone="amber">
                          {label}
                        </Badge>
                      ))}
                    </div>
                  )}
                  {role.description && <p className="mt-1 text-sm text-fg-2">{role.description}</p>}
                  {!role.description && profileLabels(role.profile).length === 0 && (
                    <p className="mt-0.5 text-sm text-fg-4">No preferences yet</p>
                  )}
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
        title={editing === 'new' ? 'New role' : `Edit ${editing?.title ?? 'role'}`}
        onClose={() => setEditing(null)}
        wide
      >
        {editing && (
          <RoleForm
            role={editing === 'new' ? null : editing}
            onClose={() => setEditing(null)}
            isAdmin={isAdmin}
          />
        )}
      </Dialog>
    </>
  )
}
