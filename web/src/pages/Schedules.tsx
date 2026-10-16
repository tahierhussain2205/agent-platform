import { useEffect, useState, type FormEvent } from 'react'
import {
  json,
  useApi,
  useApiMutation,
  useMembers,
  useScheduleOptions,
  useSchedules,
  type Schedule,
  type ScheduleKind,
  type ScheduleOptions,
} from '../api/hooks'
import { useMember } from '../components/member'
import { fromCron, toCron, type Cadence, type Preset } from './cadence'
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

const PAUSED: Record<NonNullable<Schedule['pause_reason']>, string> = {
  owner_removed: 'Owner removed',
  access_lost: 'Access lost',
  budget: 'Budget used up',
  admin: 'Paused',
}

const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

function when(iso: string, timezone: string): string {
  try {
    return new Intl.DateTimeFormat('en-GB', {
      weekday: 'short',
      day: 'numeric',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      timeZone: timezone,
    }).format(new Date(iso))
  } catch {
    return iso
  }
}

function zones(current: string): string[] {
  const all = (Intl as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf?.(
    'timeZone',
  )
  const list = all ?? ['UTC']
  return list.includes(current) ? list : [current, ...list]
}

function report(s: Schedule): string {
  if (s.kind === 'jira_progress') return `Jira progress of ${s.project ?? '?'}`
  return `Summary of ${s.channels.map((c) => `#${c.name ?? 'deleted channel'}`).join(', ')}`
}

interface FormValue {
  name: string
  kind: ScheduleKind
  channels: string[]
  project: string
  cadence: Cadence
  timezone: string
  delivery: 'dm' | 'channel'
  delivery_channel_id: string
}

function initial(options: ScheduleOptions, schedule?: Schedule): FormValue {
  if (schedule) {
    return {
      name: schedule.name,
      kind: schedule.kind === 'jira_progress' ? 'jira_progress' : 'channel_summary',
      channels: schedule.channels.map((c) => c.id),
      project: schedule.project ?? '',
      cadence: fromCron(schedule.cron),
      timezone: schedule.timezone,
      delivery: schedule.delivery.type,
      delivery_channel_id: schedule.delivery.channel_id ?? '',
    }
  }
  return {
    name: '',
    kind: 'channel_summary',
    channels: [],
    project: '',
    cadence: { preset: 'weekdays', time: '09:00', day: '1', cron: '0 9 * * 1-5' },
    timezone: options.timezone,
    delivery: 'dm',
    delivery_channel_id: '',
  }
}

function Preview({ cron, timezone }: { cron: string; timezone: string }) {
  const api = useApi()
  const [state, setState] = useState<{ text: string; runs: string[]; error?: string } | null>(null)
  useEffect(() => {
    let live = true
    const timer = setTimeout(() => {
      api<{ description: string; next_runs: string[] }>('/api/schedules/preview', {
        method: 'POST',
        ...json({ cron, timezone }),
      })
        .then((r) => live && setState({ text: r.description, runs: r.next_runs }))
        .catch((e: unknown) => {
          if (live)
            setState({ text: '', runs: [], error: e instanceof Error ? e.message : 'Invalid' })
        })
    }, 250)
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [api, cron, timezone])
  if (!state) return null
  if (state.error)
    return (
      <p className="text-xs text-bad" role="alert" data-testid="cadence-preview">
        {state.error}
      </p>
    )
  return (
    <p className="text-xs text-fg-2" data-testid="cadence-preview">
      {state.text}. Next:{' '}
      {state.runs
        .slice(0, 3)
        .map((r) => when(r, timezone))
        .join(' · ')}
    </p>
  )
}

function ScheduleForm({
  options,
  schedule,
  onDone,
}: {
  options: ScheduleOptions
  schedule?: Schedule
  onDone: () => void
}) {
  const [value, setValue] = useState<FormValue>(() => initial(options, schedule))
  const set = (patch: Partial<FormValue>) => setValue((v) => ({ ...v, ...patch }))
  const setCadence = (patch: Partial<Cadence>) => set({ cadence: { ...value.cadence, ...patch } })
  const save = useApiMutation(
    (api, body: Record<string, unknown>) =>
      schedule
        ? api(`/api/schedules/${schedule.id}`, { method: 'PATCH', ...json(body) })
        : api('/api/schedules', { method: 'POST', ...json(body) }),
    ['schedules'],
  )
  const cron = toCron(value.cadence)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    const body: Record<string, unknown> = {
      name: value.name,
      kind: value.kind,
      cron,
      timezone: value.timezone,
      delivery: value.delivery,
      channels: value.kind === 'channel_summary' ? value.channels : [],
      project: value.kind === 'jira_progress' ? value.project.toUpperCase() : null,
      delivery_channel_id: value.delivery === 'channel' ? value.delivery_channel_id || null : null,
    }
    save.mutate(body, { onSuccess: onDone })
  }
  const toggleChannel = (id: string, on: boolean) =>
    set({ channels: on ? [...value.channels, id] : value.channels.filter((c) => c !== id) })
  return (
    <form className="space-y-4" onSubmit={submit}>
      <ErrorNotice error={save.error} />
      <Field label="Name" htmlFor="schedule-name">
        <Input
          id="schedule-name"
          required
          maxLength={120}
          value={value.name}
          onChange={(e) => set({ name: e.target.value })}
        />
      </Field>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium text-fg">Report</legend>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="radio"
            name="kind"
            checked={value.kind === 'channel_summary'}
            onChange={() => set({ kind: 'channel_summary' })}
          />
          Channel summary
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="radio"
            name="kind"
            checked={value.kind === 'jira_progress'}
            disabled={!options.jira_enabled}
            onChange={() => set({ kind: 'jira_progress' })}
          />
          Jira progress{!options.jira_enabled && ' (enable Jira first)'}
        </label>
      </fieldset>
      {value.kind === 'channel_summary' ? (
        <fieldset className="space-y-1">
          <legend className="text-sm font-medium text-fg">Channels (up to 5)</legend>
          {options.channels.length === 0 && (
            <p className="text-xs text-fg-3">You can't read any channel through the agent yet.</p>
          )}
          <div className="grid max-h-40 grid-cols-2 gap-1 overflow-y-auto">
            {options.channels.map((c) => (
              <label key={c.id} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  aria-label={`Summarize #${c.name}`}
                  checked={value.channels.includes(c.id)}
                  disabled={!value.channels.includes(c.id) && value.channels.length >= 5}
                  onChange={(e) => toggleChannel(c.id, e.target.checked)}
                />
                {c.is_private ? '🔒' : '#'}
                {c.name}
              </label>
            ))}
          </div>
        </fieldset>
      ) : (
        <Field label="Jira project key" htmlFor="schedule-project">
          <Input
            id="schedule-project"
            required
            placeholder="PAY"
            maxLength={10}
            value={value.project}
            onChange={(e) => set({ project: e.target.value })}
          />
        </Field>
      )}
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Cadence" htmlFor="schedule-preset">
          <Select
            id="schedule-preset"
            value={value.cadence.preset}
            onChange={(e) => setCadence({ preset: e.target.value as Preset })}
          >
            <option value="weekdays">Every weekday</option>
            <option value="daily">Every day</option>
            <option value="weekly">Every week</option>
            <option value="custom">Custom (cron)</option>
          </Select>
        </Field>
        {value.cadence.preset === 'custom' ? (
          <Field label="Cron" htmlFor="schedule-cron" hint="minute hour day month weekday">
            <Input
              id="schedule-cron"
              required
              value={value.cadence.cron}
              onChange={(e) => setCadence({ cron: e.target.value })}
            />
          </Field>
        ) : (
          <Field label="Time" htmlFor="schedule-time">
            <Input
              id="schedule-time"
              type="time"
              required
              value={value.cadence.time}
              onChange={(e) => setCadence({ time: e.target.value })}
            />
          </Field>
        )}
        {value.cadence.preset === 'weekly' && (
          <Field label="Day" htmlFor="schedule-day">
            <Select
              id="schedule-day"
              value={value.cadence.day}
              onChange={(e) => setCadence({ day: e.target.value })}
            >
              {DAYS.map((d, i) => (
                <option key={d} value={String(i)}>
                  {d}
                </option>
              ))}
            </Select>
          </Field>
        )}
      </div>
      <Field label="Time zone" htmlFor="schedule-timezone">
        <Select
          id="schedule-timezone"
          value={value.timezone}
          onChange={(e) => set({ timezone: e.target.value })}
        >
          {zones(value.timezone).map((z) => (
            <option key={z} value={z}>
              {z}
            </option>
          ))}
        </Select>
      </Field>
      <Preview cron={cron} timezone={value.timezone} />
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium text-fg">Delivery</legend>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="radio"
            name="delivery"
            checked={value.delivery === 'dm'}
            onChange={() => set({ delivery: 'dm' })}
          />
          Direct message to the owner
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="radio"
            name="delivery"
            checked={value.delivery === 'channel'}
            onChange={() => set({ delivery: 'channel' })}
          />
          Post in a channel
        </label>
        {value.delivery === 'channel' && (
          <Select
            aria-label="Delivery channel"
            required
            value={value.delivery_channel_id}
            onChange={(e) => set({ delivery_channel_id: e.target.value })}
          >
            <option value="">Choose a channel…</option>
            {options.delivery_channels.map((c) => (
              <option key={c.id} value={c.id}>
                #{c.name}
              </option>
            ))}
          </Select>
        )}
      </fieldset>
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" busy={save.isPending}>
          {schedule ? 'Save changes' : 'Create schedule'}
        </Button>
      </div>
    </form>
  )
}

function Reassign({ schedule, onDone }: { schedule: Schedule; onDone: () => void }) {
  const members = useMembers()
  const [owner, setOwner] = useState('')
  const move = useApiMutation(
    (api, id: string) =>
      api(`/api/schedules/${schedule.id}/reassign`, {
        method: 'POST',
        ...json({ owner_membership_id: id }),
      }),
    ['schedules'],
  )
  const active = (members.data ?? []).filter(
    (m) => m.status === 'active' && m.id !== schedule.owner.membership_id,
  )
  return (
    <div className="space-y-4">
      <p className="text-sm text-fg-2">
        The schedule keeps its status. Resuming it checks the new owner&apos;s access.
      </p>
      <ErrorNotice error={move.error} />
      <Select aria-label="New owner" value={owner} onChange={(e) => setOwner(e.target.value)}>
        <option value="">Choose a member…</option>
        {active.map((m) => (
          <option key={m.id} value={m.id}>
            {m.email}
          </option>
        ))}
      </Select>
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button
          disabled={!owner}
          busy={move.isPending}
          onClick={() => move.mutate(owner, { onSuccess: onDone })}
        >
          Reassign
        </Button>
      </div>
    </div>
  )
}

function ScheduleRow({
  schedule,
  isAdmin,
  onEdit,
  onReassign,
}: {
  schedule: Schedule
  isAdmin: boolean
  onEdit: () => void
  onReassign: () => void
}) {
  const path = `/api/schedules/${schedule.id}`
  const pause = useApiMutation((api) => api(`${path}/pause`, { method: 'POST' }), ['schedules'])
  const resume = useApiMutation((api) => api(`${path}/resume`, { method: 'POST' }), ['schedules'])
  const run = useApiMutation((api) => api(`${path}/run`, { method: 'POST' }), ['schedules'])
  const remove = useApiMutation((api) => api(path, { method: 'DELETE' }), ['schedules'])
  const [confirm, setConfirm] = useState(false)
  const error = pause.error ?? resume.error ?? run.error ?? remove.error
  const active = schedule.status === 'active'
  return (
    <li
      aria-label={schedule.name}
      data-testid={`schedule-${schedule.id}`}
      className="space-y-2 rounded-lg bg-surface p-4 shadow-sm ring-1 ring-line"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-semibold text-fg">{schedule.name}</h2>
            {active ? (
              <Badge tone="green">Active</Badge>
            ) : (
              <Badge tone={schedule.pause_reason === 'admin' ? 'neutral' : 'amber'}>
                {PAUSED[schedule.pause_reason ?? 'admin']}
              </Badge>
            )}
          </div>
          <p className="text-sm text-fg-2">{report(schedule)}</p>
          <p className="text-xs text-fg-2">
            {schedule.description} ({schedule.timezone}) ·{' '}
            {schedule.delivery.type === 'dm'
              ? 'by direct message'
              : `in #${schedule.delivery.channel_name ?? '?'}`}
            {isAdmin && <> · owner {schedule.owner.email}</>}
          </p>
          {active && schedule.next_runs[0] && (
            <p className="text-xs text-fg-3">
              Next run: {when(schedule.next_runs[0], schedule.timezone)}
            </p>
          )}
          {!active && schedule.pause_detail && (
            <p className="text-xs text-warn">{schedule.pause_detail}</p>
          )}
          {schedule.last_run_at && (
            <p className="text-xs text-fg-3">
              Last run: {when(schedule.last_run_at, schedule.timezone)}
            </p>
          )}
        </div>
        {schedule.can_manage && (
          <div className="flex flex-wrap gap-2">
            {active ? (
              <>
                <Button
                  variant="secondary"
                  busy={run.isPending}
                  onClick={() => run.mutate(undefined)}
                >
                  Run now
                </Button>
                <Button
                  variant="secondary"
                  busy={pause.isPending}
                  onClick={() => pause.mutate(undefined)}
                >
                  Pause
                </Button>
              </>
            ) : (
              <Button busy={resume.isPending} onClick={() => resume.mutate(undefined)}>
                Resume
              </Button>
            )}
            <Button variant="ghost" onClick={onEdit}>
              Edit
            </Button>
            {isAdmin && (
              <Button variant="ghost" onClick={onReassign}>
                Reassign
              </Button>
            )}
            <Button variant="danger" onClick={() => setConfirm(true)}>
              Delete
            </Button>
          </div>
        )}
      </div>
      <ErrorNotice
        error={error}
        onDismiss={() => {
          pause.reset()
          resume.reset()
          run.reset()
          remove.reset()
        }}
      />
      {run.isSuccess && (
        <p role="status" className="text-sm text-ok">
          Running now: the report follows shortly.
        </p>
      )}
      <Dialog open={confirm} title={`Delete ${schedule.name}`} onClose={() => setConfirm(false)}>
        <div className="space-y-4">
          <p className="text-sm text-fg-2">The schedule stops for good.</p>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setConfirm(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              busy={remove.isPending}
              onClick={() => remove.mutate(undefined, { onSettled: () => setConfirm(false) })}
            >
              Delete
            </Button>
          </div>
        </div>
      </Dialog>
    </li>
  )
}

export function SchedulesPage() {
  const me = useMember()
  const isAdmin = me.membership.privilege === 'admin'
  const list = useSchedules()
  const options = useScheduleOptions()
  const [editing, setEditing] = useState<Schedule | 'new' | null>(null)
  const [reassigning, setReassigning] = useState<Schedule | null>(null)
  if (list.isPending || options.isPending) return <Spinner />
  if (list.error ?? options.error) return <ErrorNotice error={list.error ?? options.error} />
  const { schedules, limits } = list.data
  const opts = options.data
  return (
    <>
      <PageHeader
        title="Scheduled reports"
        description="Reports run as their owner: they only cover what the owner can read, and pause when that changes. Figures are counted by code."
        actions={
          <Button disabled={!opts.has_slack_account} onClick={() => setEditing('new')}>
            New schedule
          </Button>
        }
      />
      {!opts.has_slack_account && (
        <p className="mb-4 text-sm text-warn">
          Say hello to the agent in Slack first: reports run with your Slack account.
        </p>
      )}
      {isAdmin && limits && (
        <p className="mb-4 text-xs text-fg-2" data-testid="schedule-limits">
          {limits.active} of {limits.max_schedules} active schedules in the organization.
        </p>
      )}
      {schedules.length === 0 ? (
        <Card>
          <EmptyState>
            No scheduled reports yet. Create one here, or ask the agent in Slack: &quot;every
            weekday at 9 send me a summary of #bugs&quot;.
          </EmptyState>
        </Card>
      ) : (
        <ul className="space-y-3">
          {schedules.map((s) => (
            <ScheduleRow
              key={s.id}
              schedule={s}
              isAdmin={isAdmin}
              onEdit={() => setEditing(s)}
              onReassign={() => setReassigning(s)}
            />
          ))}
        </ul>
      )}
      <Dialog
        open={editing !== null}
        title={editing === 'new' ? 'New scheduled report' : 'Edit scheduled report'}
        onClose={() => setEditing(null)}
      >
        {editing !== null && (
          <ScheduleForm
            key={editing === 'new' ? 'new' : editing.id}
            options={opts}
            schedule={editing === 'new' ? undefined : editing}
            onDone={() => setEditing(null)}
          />
        )}
      </Dialog>
      <Dialog
        open={reassigning !== null}
        title={`Reassign ${reassigning?.name ?? ''}`}
        onClose={() => setReassigning(null)}
      >
        {reassigning && <Reassign schedule={reassigning} onDone={() => setReassigning(null)} />}
      </Dialog>
    </>
  )
}
