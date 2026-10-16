import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useAuth } from '../auth/context'
import {
  json,
  useApiMutation,
  useCatalog,
  useCatalogOptions,
  useCatalogProposal,
  type CatalogOptions,
  type CatalogOwner,
  type CatalogProposal,
  type CatalogService,
} from '../api/hooks'
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
  TextArea,
} from '../components/ui'

const SOURCES: Record<CatalogOwner['source'], string> = {
  manual: 'Set by an admin',
  codeowners: 'CODEOWNERS',
  component_lead: 'Jira component lead',
}

function Owners({ owners }: { owners: CatalogOwner[] }) {
  if (owners.length === 0) return <p className="text-sm text-amber-700">No owners yet</p>
  return (
    <ul className="space-y-1" aria-label="Owners">
      {owners.map((o) => (
        <li key={o.membership_id} className="text-sm text-slate-700">
          <span className="font-medium">{o.name ?? o.email}</span>{' '}
          <span className="text-slate-500">{o.email}</span> <Badge>{SOURCES[o.source]}</Badge>
          {!o.active && (
            <>
              {' '}
              <Badge tone="red">Not an active member</Badge>
            </>
          )}
          {o.reason && <span className="block text-xs text-slate-500">{o.reason}</span>}
        </li>
      ))}
    </ul>
  )
}

function Links({ service }: { service: CatalogService }) {
  const parts = [
    service.repository && `Repository ${service.repository.full_name}`,
    service.jira_project &&
      `Jira ${service.jira_project}${service.jira_component ? ` / ${service.jira_component}` : ''}`,
    service.paths.length > 0 && `Paths ${service.paths.join(', ')}`,
    service.log_source && `Logs ${service.log_source.project}`,
    service.channels.length > 0 &&
      `Channels ${service.channels.map((c) => `#${c.name}`).join(', ')}`,
  ].filter(Boolean)
  if (parts.length === 0) return null
  return <p className="text-xs text-slate-600">{parts.join(' · ')}</p>
}

function ServiceRow({
  service,
  canManage,
  onEdit,
}: {
  service: CatalogService
  canManage: boolean
  onEdit: () => void
}) {
  const confirm = useApiMutation<void>(
    (api) => api(`/api/catalog/${service.id}/confirm`, { method: 'POST' }),
    ['catalog'],
  )
  const archive = useApiMutation<void>(
    (api) => api(`/api/catalog/${service.id}/archive`, { method: 'POST' }),
    ['catalog'],
  )
  return (
    <li aria-label={service.name} className="space-y-2 px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="font-medium text-slate-900">
            {service.name}{' '}
            {service.status === 'proposed' ? (
              <Badge tone="amber">Proposed</Badge>
            ) : (
              <Badge tone="green">Confirmed</Badge>
            )}
          </p>
          {service.description && <p className="text-sm text-slate-600">{service.description}</p>}
          <Links service={service} />
        </div>
        {canManage && (
          <div className="flex gap-2">
            {service.status === 'proposed' && (
              <Button busy={confirm.isPending} onClick={() => confirm.mutate()}>
                Confirm
              </Button>
            )}
            <Button variant="secondary" onClick={onEdit}>
              Edit
            </Button>
            <Button
              variant="secondary"
              busy={archive.isPending}
              onClick={() => {
                if (window.confirm(`Archive ${service.name}? It will not be proposed again.`))
                  archive.mutate()
              }}
            >
              Archive
            </Button>
          </div>
        )}
      </div>
      <Owners owners={service.owners} />
      {service.evidence.length > 0 && (
        <details className="text-xs text-slate-600">
          <summary className="cursor-pointer">Why this was proposed</summary>
          <ul className="mt-1 list-disc pl-5">
            {service.evidence.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </details>
      )}
      <ErrorNotice error={confirm.error ?? archive.error} />
    </li>
  )
}

interface FormValue {
  name: string
  description: string
  repository_id: string
  jira_project: string
  jira_component: string
  paths: string
  owners: string[]
  channels: string[]
  log_project: string
  log_resource: string
  log_label: string
  log_value: string
}

function initial(service?: CatalogService): FormValue {
  return {
    name: service?.name ?? '',
    description: service?.description ?? '',
    repository_id: service?.repository?.id ?? '',
    jira_project: service?.jira_project ?? '',
    jira_component: service?.jira_component ?? '',
    paths: (service?.paths ?? []).join('\n'),
    owners: (service?.owners ?? []).map((o) => o.membership_id),
    channels: (service?.channels ?? []).map((c) => c.id),
    log_project: service?.log_source?.project ?? '',
    log_resource: service?.log_source?.resource_type ?? '',
    log_label: service?.log_source?.label ?? '',
    log_value: service?.log_source?.value ?? '',
  }
}

function toggle(list: string[], id: string, on: boolean): string[] {
  return on ? [...list, id] : list.filter((x) => x !== id)
}

function ServiceForm({
  service,
  options,
  onDone,
}: {
  service?: CatalogService
  options: CatalogOptions
  onDone: () => void
}) {
  const [value, setValue] = useState<FormValue>(() => initial(service))
  const save = useApiMutation<FormValue>(
    (api, v) =>
      api(service ? `/api/catalog/${service.id}` : '/api/catalog', {
        method: service ? 'PATCH' : 'POST',
        ...json({
          name: v.name,
          description: v.description || null,
          repository_id: v.repository_id || null,
          jira_project: v.jira_project || null,
          jira_component: v.jira_component,
          paths: v.paths.split('\n'),
          owners: v.owners,
          channels: v.channels,
          log_source: v.log_project
            ? {
                project: v.log_project.trim(),
                ...(v.log_resource.trim() ? { resource_type: v.log_resource.trim() } : {}),
                ...(v.log_label.trim() ? { label: v.log_label.trim() } : {}),
                ...(v.log_value.trim() ? { value: v.log_value.trim() } : {}),
              }
            : null,
        }),
      }),
    ['catalog'],
  )
  const submit = (e: FormEvent) => {
    e.preventDefault()
    save.mutate(value, { onSuccess: onDone })
  }
  const set = (patch: Partial<FormValue>) => setValue((v) => ({ ...v, ...patch }))
  // Owners who are no longer active members stay listed so they can be removed.
  const members = [
    ...options.members,
    ...(service?.owners ?? [])
      .filter((o) => !options.members.some((m) => m.membership_id === o.membership_id))
      .map((o) => ({ membership_id: o.membership_id, email: o.email, name: o.name })),
  ]
  return (
    <form onSubmit={submit} className="space-y-4">
      <Field label="Name" htmlFor="svc-name">
        <Input
          id="svc-name"
          required
          maxLength={120}
          value={value.name}
          onChange={(e) => set({ name: e.target.value })}
        />
      </Field>
      <Field label="Description" htmlFor="svc-description">
        <Input
          id="svc-description"
          value={value.description}
          onChange={(e) => set({ description: e.target.value })}
        />
      </Field>
      <Field label="Repository" htmlFor="svc-repo">
        <Select
          id="svc-repo"
          value={value.repository_id}
          onChange={(e) => set({ repository_id: e.target.value })}
        >
          <option value="">None</option>
          {options.repositories.map((r) => (
            <option key={r.id} value={r.id}>
              {r.full_name}
            </option>
          ))}
        </Select>
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Jira project" htmlFor="svc-project" hint="A key like PAY">
          <Input
            id="svc-project"
            value={value.jira_project}
            onChange={(e) => set({ jira_project: e.target.value.toUpperCase() })}
          />
        </Field>
        <Field label="Jira component" htmlFor="svc-component">
          <Input
            id="svc-component"
            value={value.jira_component}
            onChange={(e) => set({ jira_component: e.target.value })}
          />
        </Field>
      </div>
      <Field label="Paths" htmlFor="svc-paths" hint="Directories in the repository, one per line">
        <TextArea
          id="svc-paths"
          rows={3}
          value={value.paths}
          onChange={(e) => set({ paths: e.target.value })}
        />
      </Field>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium text-slate-700">Logs (Google Cloud Logging)</legend>
        <div className="grid grid-cols-2 gap-3">
          <Field
            label="Log project"
            htmlFor="svc-log-project"
            hint="The Google Cloud project, e.g. acme-prod"
          >
            <Input
              id="svc-log-project"
              value={value.log_project}
              onChange={(e) => set({ log_project: e.target.value })}
            />
          </Field>
          <Field label="Resource type" htmlFor="svc-log-resource" hint="e.g. cloud_run_revision">
            <Input
              id="svc-log-resource"
              value={value.log_resource}
              onChange={(e) => set({ log_resource: e.target.value })}
            />
          </Field>
          <Field label="Service label" htmlFor="svc-log-label" hint="Default: service_name">
            <Input
              id="svc-log-label"
              value={value.log_label}
              onChange={(e) => set({ log_label: e.target.value })}
            />
          </Field>
          <Field label="Label value" htmlFor="svc-log-value" hint="Default: the service's name">
            <Input
              id="svc-log-value"
              value={value.log_value}
              onChange={(e) => set({ log_value: e.target.value })}
            />
          </Field>
        </div>
      </fieldset>
      <fieldset>
        <legend className="text-sm font-medium text-slate-700">Owners</legend>
        <div className="mt-1 max-h-40 space-y-1 overflow-y-auto">
          {members.map((m) => (
            <label key={m.membership_id} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={value.owners.includes(m.membership_id)}
                onChange={(e) =>
                  set({ owners: toggle(value.owners, m.membership_id, e.target.checked) })
                }
              />
              {m.name ? `${m.name} (${m.email})` : m.email}
            </label>
          ))}
        </div>
      </fieldset>
      <fieldset>
        <legend className="text-sm font-medium text-slate-700">Channels</legend>
        <div className="mt-1 max-h-32 space-y-1 overflow-y-auto">
          {options.channels.map((c) => (
            <label key={c.id} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={value.channels.includes(c.id)}
                onChange={(e) => set({ channels: toggle(value.channels, c.id, e.target.checked) })}
              />
              #{c.name}
            </label>
          ))}
        </div>
      </fieldset>
      <ErrorNotice error={save.error} />
      <div className="flex justify-end gap-2">
        <Button variant="secondary" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" busy={save.isPending}>
          Save
        </Button>
      </div>
    </form>
  )
}

function counts(p: CatalogProposal): string {
  const parts = [
    `${p.proposed ?? 0} proposed`,
    p.refreshed ? `${p.refreshed} refreshed` : '',
    p.linked ? `${p.linked} confirmed services linked` : '',
    p.skipped ? `${p.skipped} archived names skipped` : '',
  ].filter(Boolean)
  return parts.join(', ')
}

function ProposalStatus({ proposal }: { proposal: CatalogProposal | undefined }) {
  if (!proposal || proposal.status === 'none' || proposal.status === 'unknown') return null
  if (proposal.status === 'running')
    return (
      <p className="flex items-center gap-2 text-sm text-slate-600" data-testid="proposal-status">
        <Spinner className="h-4 w-4" /> Proposing services from GitHub and Jira…
      </p>
    )
  if (proposal.status === 'failed')
    return (
      <p className="text-sm text-red-700" data-testid="proposal-status">
        The last proposal failed. Try again, or check the integrations.
      </p>
    )
  return (
    <div className="text-sm text-slate-600" data-testid="proposal-status">
      <p>Last proposal: {counts(proposal)}.</p>
      {(proposal.notes ?? []).length > 0 && (
        <ul className="list-disc pl-5 text-xs">
          {(proposal.notes ?? []).map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function CatalogPage() {
  const catalog = useCatalog()
  const canManage = catalog.data?.can_manage ?? false
  const options = useCatalogOptions(canManage)
  const proposal = useCatalogProposal(canManage)
  const client = useQueryClient()
  const org = useAuth().activeOrgId ?? 'none'
  const [editing, setEditing] = useState<CatalogService | 'new' | null>(null)
  const propose = useApiMutation<void>(
    (api) => api('/api/catalog/propose', { method: 'POST' }),
    ['catalog'],
  )
  // When a running proposal finishes, show the services it proposed.
  const previous = useRef(proposal.data?.status)
  useEffect(() => {
    const status = proposal.data?.status
    if (previous.current === 'running' && status && status !== 'running')
      void client.invalidateQueries({ queryKey: [org, 'catalog'], exact: true })
    previous.current = status
  }, [client, org, proposal.data?.status])

  if (catalog.isPending) return <Spinner />
  if (catalog.error) return <ErrorNotice error={catalog.error} />
  const services = catalog.data.services
  const proposed = services.filter((s) => s.status === 'proposed')
  const confirmed = services.filter((s) => s.status === 'active')
  const running = proposal.data?.status === 'running'
  return (
    <div className="space-y-6">
      <PageHeader
        title="Service catalog"
        description="Who owns which service, and why. The agent suggests ticket assignees only from confirmed services."
        actions={
          canManage && (
            <div className="flex gap-2">
              <Button
                variant="secondary"
                busy={propose.isPending || running}
                onClick={() =>
                  propose.mutate(undefined, { onSuccess: () => void proposal.refetch() })
                }
              >
                Propose services
              </Button>
              <Button onClick={() => setEditing('new')}>Add service</Button>
            </div>
          )
        }
      />
      <ErrorNotice error={propose.error} onDismiss={() => propose.reset()} />
      {canManage && <ProposalStatus proposal={proposal.data} />}
      {proposed.length > 0 && (
        <Card title="Proposed services">
          <p className="px-4 pt-3 text-sm text-slate-600">
            Proposed from GitHub CODEOWNERS and Jira components you can read. Confirm a service to
            let the agent suggest its owners; edit it first if something is off.
          </p>
          <ul className="divide-y divide-slate-100" aria-label="Proposed services">
            {proposed.map((s) => (
              <ServiceRow
                key={s.id}
                service={s}
                canManage={canManage}
                onEdit={() => setEditing(s)}
              />
            ))}
          </ul>
        </Card>
      )}
      <Card title="Confirmed services">
        {confirmed.length === 0 ? (
          <EmptyState>
            No confirmed services yet.
            {canManage && ' Propose services from GitHub and Jira, or add one.'}
          </EmptyState>
        ) : (
          <ul className="divide-y divide-slate-100" aria-label="Confirmed services">
            {confirmed.map((s) => (
              <ServiceRow
                key={s.id}
                service={s}
                canManage={canManage}
                onEdit={() => setEditing(s)}
              />
            ))}
          </ul>
        )}
      </Card>
      <Dialog
        open={editing !== null}
        title={editing === 'new' ? 'Add a service' : `Edit ${editing?.name ?? ''}`}
        onClose={() => setEditing(null)}
      >
        {editing !== null &&
          (options.data ? (
            <ServiceForm
              key={editing === 'new' ? 'new' : editing.id}
              service={editing === 'new' ? undefined : editing}
              options={options.data}
              onDone={() => setEditing(null)}
            />
          ) : (
            <Spinner />
          ))}
      </Dialog>
    </div>
  )
}
