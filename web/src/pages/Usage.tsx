import { useState, type ReactNode } from 'react'
import { useUsage, type Usage } from '../api/hooks'
import { Card, EmptyState, ErrorNotice, PageHeader, Select, Spinner } from '../components/ui'

const WINDOWS = [7, 30, 90]
const tokens = new Intl.NumberFormat('en-US')
const dollars = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

export function UsagePage() {
  const [days, setDays] = useState(30)
  const usage = useUsage(days)
  return (
    <>
      <PageHeader
        title="Usage"
        description="Model tokens and cost for this organization (days in UTC, as the limits count them)."
        actions={
          <Select
            aria-label="Window"
            className="max-w-40"
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
          >
            {WINDOWS.map((d) => (
              <option key={d} value={d}>
                Last {d} days
              </option>
            ))}
          </Select>
        }
      />
      {usage.isPending ? (
        <Spinner />
      ) : usage.error ? (
        <ErrorNotice error={usage.error} />
      ) : (
        <UsageReport usage={usage.data} />
      )}
    </>
  )
}

function UsageReport({ usage }: { usage: Usage }) {
  const { limits, today, month } = usage
  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2">
        <Card title="This month">
          <Meter label="Tokens" used={month.tokens} limit={limits.org_monthly_token_limit} />
          <p className="mt-2 text-sm text-slate-600">{dollars.format(month.cost)} so far</p>
        </Card>
        <Card title="Today">
          <p className="text-2xl font-semibold text-slate-900" data-testid="today-tokens">
            {tokens.format(today.tokens)} tokens
          </p>
          <p className="mt-2 text-sm text-slate-600">
            {dollars.format(today.cost)}
            {limits.user_daily_token_limit !== null &&
              ` · each person may use ${tokens.format(limits.user_daily_token_limit)} a day`}
          </p>
        </Card>
      </div>
      <Card title="Per day">
        <Daily days={usage.daily} />
      </Card>
      <Card title="People">
        <Table
          empty="Nobody has used the agent in this window."
          head={['Person', 'Tokens', 'Today', 'Cost']}
          rows={usage.people.map((p) => [
            p.email ? (p.name ? `${p.name} (${p.email})` : p.email) : 'Scheduled and system work',
            tokens.format(p.tokens),
            limits.user_daily_token_limit !== null && p.email
              ? `${tokens.format(p.today_tokens)} of ${tokens.format(limits.user_daily_token_limit)}`
              : tokens.format(p.today_tokens),
            dollars.format(p.cost),
          ])}
        />
      </Card>
      <Card title="Playbooks">
        <Table
          empty="No tasks in this window."
          head={['Playbook', 'Tasks', 'Tokens', 'Cost']}
          rows={usage.playbooks.map((p) => [
            p.title,
            tokens.format(p.tasks),
            tokens.format(p.tokens),
            dollars.format(p.cost),
          ])}
        />
      </Card>
      <Card title="Models">
        <Table
          empty="No model calls in this window."
          head={['Model', 'Input', 'Output', 'Cached', 'Cost']}
          rows={usage.models.map((m) => [
            <span className="font-mono text-xs">{m.model}</span>,
            tokens.format(m.input_tokens),
            tokens.format(m.output_tokens),
            tokens.format(m.cached_tokens),
            dollars.format(m.cost),
          ])}
        />
      </Card>
    </div>
  )
}

function Meter({ label, used, limit }: { label: string; used: number; limit: number | null }) {
  if (limit === null) {
    return (
      <p className="text-2xl font-semibold text-slate-900">
        {tokens.format(used)} <span className="text-sm font-normal text-slate-600">no limit</span>
      </p>
    )
  }
  const share = Math.min(used / limit, 1)
  const tone = share >= 1 ? 'bg-red-500' : share >= 0.8 ? 'bg-amber-500' : 'bg-indigo-500'
  return (
    <div>
      <p className="text-2xl font-semibold text-slate-900">
        {tokens.format(used)}{' '}
        <span className="text-sm font-normal text-slate-600">of {tokens.format(limit)}</span>
      </p>
      <div
        role="meter"
        aria-label={`${label} used this month`}
        aria-valuemin={0}
        aria-valuemax={limit}
        aria-valuenow={Math.min(used, limit)}
        className="mt-2 h-2 rounded-full bg-slate-100"
      >
        <div className={`h-2 rounded-full ${tone}`} style={{ width: `${share * 100}%` }} />
      </div>
    </div>
  )
}

function Daily({ days }: { days: Usage['daily'] }) {
  const peak = Math.max(...days.map((d) => d.tokens), 1)
  if (days.every((d) => d.tokens === 0)) return <EmptyState>No usage in this window.</EmptyState>
  return (
    <div className="flex h-32 items-end gap-px" aria-label="Tokens per day">
      {days.map((d) => (
        <div
          key={d.day}
          className="flex-1 rounded-t bg-indigo-400 hover:bg-indigo-600"
          style={{ height: `${Math.max((d.tokens / peak) * 100, d.tokens ? 2 : 0)}%` }}
          title={`${d.day}: ${tokens.format(d.tokens)} tokens, ${dollars.format(d.cost)}`}
          data-testid="usage-day"
        />
      ))}
    </div>
  )
}

function Table({ head, rows, empty }: { head: string[]; rows: ReactNode[][]; empty: string }) {
  if (rows.length === 0) return <EmptyState>{empty}</EmptyState>
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-slate-500">
          <tr>
            {head.map((h, i) => (
              <th key={h} className={`pb-2 pr-3 font-medium ${i > 0 ? 'text-right' : ''}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-200">
          {rows.map((row, r) => (
            <tr key={r}>
              {row.map((cell, i) => (
                <td key={i} className={`py-2 pr-3 ${i > 0 ? 'text-right tabular-nums' : ''}`}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
