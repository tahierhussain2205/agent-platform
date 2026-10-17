import { useState, type FormEvent, type ReactNode } from 'react'
import {
  json,
  useApiMutation,
  useRolePreview,
  type PreviewSample,
  type Role,
  type RoleFocus,
  type RoleProfile,
} from '../api/hooks'
import { DETAIL, FOCUS, FORMAT, STYLE } from './roleProfile'
import { Button, ErrorNotice, Field, Input, Select, Spinner, TextArea } from './ui'

const SAMPLES: Record<PreviewSample, string> = {
  summary: 'Summarize this thread for me.',
  status: 'Where do things stand, and what should happen next?',
  fix: 'How should we fix this?',
}

/** One choice of several (or none), as a row of toggle buttons backed by radio inputs. */
function Choice<T extends string>({
  legend,
  name,
  options,
  value,
  onChange,
}: {
  legend: string
  name: string
  options: Record<T, string>
  value: T | null | undefined
  onChange: (value: T | null) => void
}) {
  const entries: [T | null, string][] = [
    [null, 'No preference'],
    ...(Object.entries(options) as [T, string][]),
  ]
  return (
    <fieldset>
      <legend className="mb-1.5 text-sm font-medium text-fg">{legend}</legend>
      <div className="flex flex-wrap gap-1.5">
        {entries.map(([key, label]) => {
          const checked = (value ?? null) === key
          return (
            <label
              key={key ?? 'none'}
              className={`rounded-md px-2.5 py-1 text-sm ring-1 ring-inset has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-fg ${
                checked
                  ? 'bg-accent font-medium text-accent-fg ring-accent'
                  : 'bg-surface text-fg-2 ring-line-strong hover:bg-subtle'
              }`}
            >
              <input
                type="radio"
                className="sr-only"
                name={name}
                checked={checked}
                onChange={() => onChange(key)}
              />
              {label}
            </label>
          )
        })}
      </div>
    </fieldset>
  )
}

function FocusChips({
  value,
  onChange,
}: {
  value: RoleFocus[]
  onChange: (value: RoleFocus[]) => void
}) {
  return (
    <fieldset>
      <legend className="mb-1.5 text-sm font-medium text-fg">Always call out</legend>
      <div className="flex flex-wrap gap-1.5">
        {(Object.entries(FOCUS) as [RoleFocus, string][]).map(([key, label]) => {
          const checked = value.includes(key)
          return (
            <label
              key={key}
              className={`rounded-full px-2.5 py-1 text-sm ring-1 ring-inset has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-fg ${
                checked
                  ? 'bg-accent font-medium text-accent-fg ring-accent'
                  : 'bg-surface text-fg-2 ring-line-strong hover:bg-subtle'
              }`}
            >
              <input
                type="checkbox"
                className="sr-only"
                checked={checked}
                onChange={(e) =>
                  onChange(e.target.checked ? [...value, key] : value.filter((f) => f !== key))
                }
              />
              {label}
            </label>
          )
        })}
      </div>
    </fieldset>
  )
}

/** Slack mrkdwn as the agent writes it: *bold*, _italic_ and line breaks. */
export function Mrkdwn({ text }: { text: string }) {
  const parts: ReactNode[] = []
  const pattern = /\*([^*\n]+)\*|_([^_\n]+)_/g
  let last = 0
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) parts.push(text.slice(last, match.index))
    parts.push(
      match[1] ? (
        <strong key={match.index}>{match[1]}</strong>
      ) : (
        <em key={match.index}>{match[2]}</em>
      ),
    )
    last = match.index + match[0].length
  }
  parts.push(text.slice(last))
  return <div className="whitespace-pre-wrap break-words text-sm leading-relaxed">{parts}</div>
}

function RolePreviewPanel({
  title,
  description,
  profile,
}: {
  title: string
  description: string
  profile: RoleProfile
}) {
  const [sample, setSample] = useState<PreviewSample>('summary')
  const [own, setOwn] = useState('')
  const [previewId, setPreviewId] = useState<string | null>(null)
  const start = useApiMutation(
    (api, body: object) =>
      api<{ id: string; request: string }>('/api/roles/preview', { method: 'POST', ...json(body) }),
    [],
  )
  const result = useRolePreview(previewId)
  const running =
    start.isPending ||
    (previewId !== null && (result.isPending || result.data?.status === 'running'))
  const run = () =>
    start.mutate(
      { title, description, profile, sample, request: own.trim() || null },
      { onSuccess: (data) => setPreviewId(data.id) },
    )
  const done = result.data?.status === 'done' ? result.data : null
  return (
    <section
      aria-label="Preview"
      className="space-y-3 rounded-lg bg-subtle p-4 ring-1 ring-inset ring-line"
    >
      <div>
        <h3 className="text-sm font-semibold text-fg">Preview</h3>
        <p className="mt-0.5 text-xs text-fg-3">
          The agent answers a sample Slack thread (a checkout bug) twice: without any role, and as
          this role with the choices above, saved or not.
        </p>
      </div>
      <div className="flex flex-col gap-2">
        <Select
          aria-label="Sample request"
          value={sample}
          onChange={(e) => setSample(e.target.value as PreviewSample)}
          disabled={own.trim() !== ''}
        >
          {(Object.entries(SAMPLES) as [PreviewSample, string][]).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </Select>
        <Input
          aria-label="Or ask your own question"
          placeholder="Or ask your own question"
          maxLength={300}
          value={own}
          onChange={(e) => setOwn(e.target.value)}
        />
        <Button variant="secondary" onClick={run} busy={running} className="self-start">
          {done ? 'Preview again' : 'Preview'}
        </Button>
      </div>
      <ErrorNotice error={start.error ?? result.error} />
      {result.data?.status === 'failed' && (
        <p className="text-sm text-bad" role="alert">
          {result.data.message}
        </p>
      )}
      {running && !done && (
        <div className="flex items-center gap-2 text-sm text-fg-3">
          <Spinner className="h-4 w-4" /> Writing both answers…
        </div>
      )}
      {done && (
        <div className="grid gap-3">
          <div
            className="rounded-md bg-surface p-3 ring-1 ring-inset ring-line-strong"
            data-testid="preview-with-role"
          >
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-fg-3">
              As {title.trim() || 'this role'}
            </p>
            <Mrkdwn text={done.with_role ?? ''} />
          </div>
          <div className="rounded-md bg-surface p-3 ring-1 ring-inset ring-line">
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-fg-3">
              Without a role
            </p>
            <Mrkdwn text={done.without ?? ''} />
          </div>
        </div>
      )}
    </section>
  )
}

export function RoleForm({
  role,
  onClose,
  isAdmin,
}: {
  role: Role | null
  onClose: () => void
  isAdmin: boolean
}) {
  const [title, setTitle] = useState(role?.title ?? '')
  const [description, setDescription] = useState(role?.description ?? '')
  const [profile, setProfile] = useState<RoleProfile>({
    detail: role?.profile?.detail ?? null,
    style: role?.profile?.style ?? null,
    format: role?.profile?.format ?? null,
    focus: role?.profile?.focus ?? [],
  })
  const save = useApiMutation(
    (api, body: { title: string; description: string; profile: RoleProfile }) =>
      role
        ? api(`/api/roles/${role.id}`, { method: 'PATCH', ...json(body) })
        : api('/api/roles', { method: 'POST', ...json(body) }),
    ['roles', 'members', 'org'],
  )
  const set = (change: Partial<RoleProfile>) => setProfile({ ...profile, ...change })
  const submit = (e: FormEvent) => {
    e.preventDefault()
    save.mutate({ title, description, profile }, { onSuccess: onClose })
  }
  return (
    <form onSubmit={submit} className="space-y-5">
      <ErrorNotice error={save.error} />
      <div
        className={isAdmin ? 'grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]' : undefined}
      >
        <div className="space-y-5">
          <Field label="Title" htmlFor="role-title">
            <Input
              id="role-title"
              required
              maxLength={80}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </Field>
          <div className="space-y-4">
            <div>
              <h3 className="text-sm font-semibold text-fg">
                How the agent answers people in this role
              </h3>
              <p className="text-xs text-fg-3">
                Tone, depth and format. Roles never grant access to anything.
              </p>
            </div>
            <Choice
              legend="Detail"
              name="detail"
              options={DETAIL}
              value={profile.detail}
              onChange={(detail) => set({ detail })}
            />
            <Choice
              legend="Language"
              name="style"
              options={STYLE}
              value={profile.style}
              onChange={(style) => set({ style })}
            />
            <Choice
              legend="Format"
              name="format"
              options={FORMAT}
              value={profile.format}
              onChange={(format) => set({ format })}
            />
            <FocusChips value={profile.focus ?? []} onChange={(focus) => set({ focus })} />
            <Field
              label="Anything else"
              htmlFor="role-description"
              hint="In your own words, for example what this role cares about."
            >
              <TextArea
                id="role-description"
                rows={3}
                maxLength={4000}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </Field>
          </div>
        </div>
        {isAdmin && (
          <div className="self-start lg:sticky lg:top-0">
            <RolePreviewPanel title={title} description={description} profile={profile} />
          </div>
        )}
      </div>
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
