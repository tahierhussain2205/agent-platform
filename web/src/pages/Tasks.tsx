import { useState } from 'react'
import { Link, useParams } from 'react-router'
import {
  isActiveTask,
  useTask,
  useTasks,
  type EventTone,
  type TaskDetail,
  type TaskEvent,
  type TaskStatus,
  type TaskSummary,
} from '../api/hooks'
import { useMember } from '../components/member'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNotice,
  PageHeader,
  Select,
  Spinner,
} from '../components/ui'

const STATUS: Record<
  TaskStatus,
  { label: string; tone: 'neutral' | 'strong' | 'green' | 'amber' | 'red' }
> = {
  queued: { label: 'Queued', tone: 'neutral' },
  running: { label: 'Running', tone: 'strong' },
  awaiting_approval: { label: 'Waiting for approval', tone: 'amber' },
  completed: { label: 'Completed', tone: 'green' },
  failed: { label: 'Failed', tone: 'red' },
  cancelled: { label: 'Cancelled', tone: 'neutral' },
}

const STEPS: { kind: string; label: string }[] = [
  { kind: 'gather', label: 'Understand and read' },
  { kind: 'plan', label: 'Plan' },
  { kind: 'act', label: 'Act' },
  { kind: 'verify', label: 'Verify' },
  { kind: 'report', label: 'Report' },
]

const PURPOSE: Record<string, string> = {
  classify: 'choose a playbook',
  brief: 'write the brief',
  gather: 'read the sources',
  plan: 'plan',
  report: 'write the answer',
  review: 'review the change',
  implement: 'implement',
  investigate: 'investigate',
  schedule: 'set up a schedule',
}

function StatusBadge({ status }: { status: TaskStatus }) {
  const s = STATUS[status] ?? { label: status, tone: 'neutral' as const }
  return <Badge tone={s.tone}>{s.label}</Badge>
}

function duration(task: TaskSummary): string {
  const start = task.started_at ?? task.created_at
  const end = task.finished_at ? new Date(task.finished_at) : new Date()
  const seconds = Math.max(0, Math.round((end.getTime() - new Date(start).getTime()) / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

function where(task: TaskSummary): string {
  if (task.trigger === 'schedule') return 'Scheduled report'
  if (task.dm) return 'Direct message'
  return task.channel ? `#${task.channel}` : 'Slack'
}

const titleOf = (task: TaskSummary) => task.title || task.playbook_title || 'Request in Slack'

export function TasksPage() {
  const me = useMember()
  const isAdmin = me.membership.privilege === 'admin'
  const [status, setStatus] = useState('')
  const [cursors, setCursors] = useState<string[]>([])
  const tasks = useTasks({ status: status || undefined, before: cursors.at(-1) })
  return (
    <>
      <PageHeader
        title="Tasks"
        description={
          isAdmin
            ? 'Every request the agent worked on, with what it read, decided and did.'
            : 'The requests you asked the agent for, with what it read, decided and did.'
        }
      />
      <Card>
        <div className="mb-4 flex flex-wrap gap-3">
          <Select
            aria-label="Filter by status"
            className="max-w-48"
            value={status}
            onChange={(e) => (setStatus(e.target.value), setCursors([]))}
          >
            <option value="">Any status</option>
            <option value="active">In progress</option>
            <option value="awaiting_approval">Waiting for approval</option>
            <option value="completed">Completed</option>
            <option value="failed">Failed</option>
            <option value="cancelled">Cancelled</option>
          </Select>
        </div>
        {tasks.isPending ? (
          <Spinner />
        ) : tasks.error ? (
          <ErrorNotice error={tasks.error} />
        ) : tasks.data.items.length === 0 ? (
          <EmptyState>
            No tasks yet. Mention the agent in an allowlisted Slack channel and its work shows up
            here.
          </EmptyState>
        ) : (
          <div className="relative overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-fg-3">
                <tr>
                  <th className="pb-2 pr-3 font-medium">Task</th>
                  {isAdmin && <th className="pb-2 pr-3 font-medium">Requested by</th>}
                  <th className="pb-2 pr-3 font-medium">Status</th>
                  <th className="pb-2 pr-3 font-medium">Started</th>
                  <th className="pb-2 pr-3 font-medium">Took</th>
                  <th className="pb-2 text-right font-medium">Tokens</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {tasks.data.items.map((task) => (
                  <tr key={task.id} data-testid="task-row">
                    <td className="max-w-md py-2.5 pr-3">
                      <Link
                        to={`/tasks/${task.id}`}
                        className="line-clamp-2 font-medium text-fg hover:underline"
                        title={titleOf(task)}
                      >
                        {titleOf(task)}
                      </Link>
                      <div className="mt-0.5 text-xs text-fg-3">
                        {[task.playbook_title, where(task), task.mode === 'auto' ? 'Auto' : 'Ask']
                          .filter(Boolean)
                          .join(' · ')}
                      </div>
                    </td>
                    {isAdmin && (
                      <td className="py-2.5 pr-3 text-fg-2">
                        {task.requester_name || task.requester || 'Schedule'}
                      </td>
                    )}
                    <td className="py-2.5 pr-3">
                      <StatusBadge status={task.status} />
                    </td>
                    <td className="whitespace-nowrap py-2.5 pr-3 text-fg-2">
                      {new Date(task.created_at).toLocaleString()}
                    </td>
                    <td className="whitespace-nowrap py-2.5 pr-3 text-fg-2">{duration(task)}</td>
                    <td className="py-2.5 text-right tabular-nums text-fg-2">
                      {task.tokens_used.toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="mt-4 flex justify-between">
          <Button
            variant="secondary"
            disabled={cursors.length === 0}
            onClick={() => setCursors(cursors.slice(0, -1))}
          >
            Newer
          </Button>
          <Button
            variant="secondary"
            disabled={!tasks.data?.next_before}
            onClick={() =>
              tasks.data?.next_before && setCursors([...cursors, tasks.data.next_before])
            }
          >
            Older
          </Button>
        </div>
      </Card>
    </>
  )
}

const DOT: Record<EventTone, string> = {
  neutral: 'bg-fg-4',
  green: 'bg-ok',
  amber: 'bg-warn',
  red: 'bg-bad',
}

const KIND_LABEL: Record<TaskEvent['kind'], string> = {
  step: 'Step',
  policy: 'Policy',
  approval: 'Approval',
  action: 'Change',
  model: 'Model',
  artifact: 'Saved',
}

type Row =
  | { kind: 'event'; event: TaskEvent }
  | { kind: 'model'; events: TaskEvent[] }
  | { kind: 'reads'; events: TaskEvent[] }

/** Runs of model calls, and of allowed reads, each read as one line: they are the agent thinking
 * and looking, not decisions anyone needs to see one by one. */
function rows(events: TaskEvent[]): Row[] {
  const out: Row[] = []
  for (const event of events) {
    const last = out.at(-1)
    const group = event.kind === 'model' ? 'model' : event.read ? 'reads' : null
    if (group && last?.kind === group) last.events.push(event)
    else if (group) out.push({ kind: group, events: [event] })
    else out.push({ kind: 'event', event })
  }
  return out
}

/** "jira.search_issues ×3, slack.read_channel #eng" */
function readsSummary(events: TaskEvent[]): string {
  const counts = new Map<string, number>()
  for (const e of events) {
    const name = e.target ?? e.title.replace(/^Policy: /, '')
    counts.set(name, (counts.get(name) ?? 0) + 1)
  }
  return [...counts].map(([name, n]) => (n > 1 ? `${name} ×${n}` : name)).join(', ')
}

function purposeOf(event: TaskEvent): string {
  const key = event.title.replace(/^Model call: /, '')
  return PURPOSE[key] ?? key
}

function Time({ at }: { at: string }) {
  return (
    <time dateTime={at} className="whitespace-nowrap text-xs tabular-nums text-fg-3">
      {new Date(at).toLocaleTimeString()}
    </time>
  )
}

function Timeline({ events }: { events: TaskEvent[] }) {
  if (events.length === 0) return <EmptyState>Nothing recorded yet.</EmptyState>
  return (
    <ol className="relative space-y-3 border-l border-line pl-5" aria-label="Timeline">
      {rows(events).map((row, i) => {
        if (row.kind === 'reads') {
          return (
            <li key={i} className="relative" data-testid="timeline-reads">
              <span className="absolute -left-[25px] top-1.5 h-2 w-2 rounded-full bg-ok" />
              <div className="flex flex-wrap items-baseline gap-x-2">
                <Time at={row.events[0]?.at ?? ''} />
                <span className="text-[11px] font-medium uppercase tracking-wide text-fg-3">
                  Read
                </span>
                <span className="text-sm text-fg-2">
                  {row.events.length === 1 ? 'Allowed read' : `${row.events.length} allowed reads`}:{' '}
                  {readsSummary(row.events)}
                </span>
              </div>
            </li>
          )
        }
        if (row.kind === 'model') {
          const tokens = row.events.reduce(
            (sum, e) => sum + (e.input_tokens ?? 0) + (e.output_tokens ?? 0),
            0,
          )
          const purposes = [...new Set(row.events.map(purposeOf))].join(', ')
          return (
            <li key={i} className="relative" data-testid="timeline-model">
              <span className="absolute -left-[25px] top-1.5 h-2 w-2 rounded-full bg-line-strong" />
              <div className="flex flex-wrap items-baseline gap-x-2">
                <Time at={row.events[0]?.at ?? ''} />
                <span className="text-xs text-fg-3">
                  {row.events.length === 1 ? 'Model call' : `${row.events.length} model calls`}:{' '}
                  {purposes} · {tokens.toLocaleString()} tokens
                </span>
              </div>
            </li>
          )
        }
        const e = row.event
        return (
          <li key={i} className="relative" data-testid={`timeline-${e.kind}`}>
            <span className={`absolute -left-[25px] top-1.5 h-2 w-2 rounded-full ${DOT[e.tone]}`} />
            <div className="flex flex-wrap items-baseline gap-x-2">
              <Time at={e.at} />
              <span className="text-[11px] font-medium uppercase tracking-wide text-fg-3">
                {KIND_LABEL[e.kind]}
              </span>
              <span className="text-sm font-medium text-fg">{e.title}</span>
            </div>
            {e.detail && <p className="mt-0.5 break-words text-sm text-fg-2">{e.detail}</p>}
            {e.kind === 'approval' && e.decides && e.status === 'pending' && (
              <p className="mt-0.5 text-xs text-fg-3">{capitalize(e.decides)} decides.</p>
            )}
          </li>
        )
      })}
    </ol>
  )
}

const capitalize = (text: string) => text.charAt(0).toUpperCase() + text.slice(1)

function Stages({ steps }: { steps: TaskDetail['steps'] }) {
  const byKind = new Map(steps.map((s) => [s.kind, s]))
  return (
    <ol className="grid grid-cols-2 gap-2 sm:grid-cols-5" aria-label="Steps">
      {STEPS.map(({ kind, label }) => {
        const step = byKind.get(kind)
        const tone =
          step?.status === 'completed'
            ? 'ring-ok-line bg-ok-soft text-ok'
            : step?.status === 'failed'
              ? 'ring-bad-line bg-bad-soft text-bad'
              : step?.status === 'running'
                ? 'ring-warn-line bg-warn-soft text-warn'
                : 'ring-line bg-subtle text-fg-3'
        return (
          <li key={kind} className={`rounded-md px-3 py-2 text-xs ring-1 ring-inset ${tone}`}>
            <div className="font-medium">{label}</div>
            <div className="mt-0.5 truncate opacity-90" title={step?.summary ?? undefined}>
              {step ? (step.summary ?? step.status) : 'Not needed'}
            </div>
          </li>
        )
      })}
    </ol>
  )
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-fg-3">{label}</dt>
      <dd className="mt-0.5 text-sm text-fg">{children}</dd>
    </div>
  )
}

export function TaskDetailPage() {
  const { taskId = '' } = useParams()
  const detail = useTask(taskId)
  if (detail.isPending) return <Spinner />
  if (detail.error) {
    return (
      <>
        <BackLink />
        <ErrorNotice error={detail.error} />
      </>
    )
  }
  const { task, steps, links, usage, events, approvals } = detail.data
  const pending = approvals.filter((a) => a.status === 'pending')
  return (
    <>
      <BackLink />
      <PageHeader
        title={titleOf(task)}
        description={[task.playbook_title, where(task)].filter(Boolean).join(' · ')}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={task.status} />
            <Badge tone={task.mode === 'auto' ? 'strong' : 'neutral'}>
              {task.mode === 'auto' ? 'Auto' : 'Ask'}
            </Badge>
            {task.slack_url && (
              <a
                href={task.slack_url}
                target="_blank"
                rel="noreferrer"
                className="text-sm font-medium text-fg underline-offset-2 hover:underline"
              >
                Open in Slack ↗
              </a>
            )}
          </div>
        }
      />
      <div className="space-y-6">
        {pending.length > 0 && (
          <div
            className="rounded-md bg-warn-soft px-4 py-3 text-sm text-warn ring-1 ring-inset ring-warn-line"
            role="status"
          >
            {pending.map((a) => (
              <p key={a.id}>
                Waiting for approval: {a.action_summary.split('\n')[0]} ({a.decides} decides)
              </p>
            ))}
          </div>
        )}
        {task.error && (
          <div className="rounded-md bg-bad-soft px-4 py-3 text-sm text-bad ring-1 ring-inset ring-bad-line">
            {task.error}
          </div>
        )}
        <Card>
          <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <Fact label="Requested by">{task.requester_name || task.requester || 'Schedule'}</Fact>
            <Fact label="Started">{new Date(task.created_at).toLocaleString()}</Fact>
            <Fact label="Took">{duration(task)}</Fact>
            <Fact label="Model use">
              {usage.calls > 0 && `${usage.calls} call${usage.calls === 1 ? '' : 's'} · `}
              {task.tokens_used.toLocaleString()} tokens
            </Fact>
          </dl>
          {links.length > 0 && (
            <div className="mt-4 flex flex-wrap gap-2 border-t border-line pt-4">
              {links.map((link) =>
                link.url ? (
                  <a
                    key={link.label}
                    href={link.url}
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-md bg-muted px-2.5 py-1 text-sm font-medium text-fg ring-1 ring-inset ring-line hover:bg-subtle"
                  >
                    {link.kind === 'pull_request' ? 'Pull request' : 'Jira'} {link.label} ↗
                  </a>
                ) : (
                  <span
                    key={link.label}
                    className="rounded-md bg-muted px-2.5 py-1 text-sm text-fg-2 ring-1 ring-inset ring-line"
                  >
                    {link.kind === 'pull_request' ? 'Pull request' : 'Jira'} {link.label}
                  </span>
                ),
              )}
            </div>
          )}
        </Card>
        <Card title="Steps">
          <Stages steps={steps} />
        </Card>
        <Card
          title="Timeline"
          actions={
            isActiveTask(task.status) ? (
              <span className="text-xs text-fg-3">Updating while the task runs</span>
            ) : undefined
          }
        >
          <Timeline events={events} />
        </Card>
      </div>
    </>
  )
}

function BackLink() {
  return (
    <Link to="/tasks" className="mb-3 inline-block text-sm text-fg-2 hover:text-fg">
      ← All tasks
    </Link>
  )
}
