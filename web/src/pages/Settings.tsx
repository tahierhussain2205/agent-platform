import { useState, type FormEvent } from 'react'
import { ApiRequestError } from '../api/client'
import { json, useApiMutation, useSettings, type OrgSettings, type Tier } from '../api/hooks'
import {
  Button,
  Card,
  ErrorNotice,
  Field,
  Input,
  PageHeader,
  Select,
  Spinner,
} from '../components/ui'

const TIERS: { value: Tier; label: string }[] = [
  { value: 'fast', label: 'Fast' },
  { value: 'standard', label: 'Standard' },
  { value: 'advanced', label: 'Advanced' },
]

interface FormState {
  default_model_tier: Tier
  allowed_model_tiers: Tier[]
  user_daily_token_limit: string
  org_monthly_token_limit: string
  max_concurrent_tasks: string
  max_schedules: string
  retention_days: string
}

const toForm = (s: OrgSettings): FormState => ({
  default_model_tier: s.default_model_tier,
  allowed_model_tiers: s.allowed_model_tiers,
  user_daily_token_limit: s.user_daily_token_limit?.toString() ?? '',
  org_monthly_token_limit: s.org_monthly_token_limit?.toString() ?? '',
  max_concurrent_tasks: s.max_concurrent_tasks.toString(),
  max_schedules: s.max_schedules.toString(),
  retention_days: s.retention_days.toString(),
})

export function SettingsPage() {
  const settings = useSettings()
  // Lives here, not in the form: the form remounts after a save (see the key below).
  const [saved, setSaved] = useState(false)
  if (settings.error) return <ErrorNotice error={settings.error} />
  if (settings.isPending) return <Spinner />
  // Keyed by updated_at: after a save the form starts again from the stored values.
  return (
    <SettingsForm
      key={settings.data.updated_at}
      initial={settings.data}
      saved={saved}
      setSaved={setSaved}
    />
  )
}

function SettingsForm({
  initial,
  saved,
  setSaved,
}: {
  initial: OrgSettings
  saved: boolean
  setSaved: (value: boolean) => void
}) {
  const [form, setForm] = useState<FormState>(() => toForm(initial))
  const save = useApiMutation(
    (api, body: Record<string, unknown>) =>
      api<OrgSettings>('/api/settings', { method: 'PATCH', ...json(body) }),
    ['settings'],
  )
  const fieldError = (name: string) =>
    save.error instanceof ApiRequestError
      ? save.error.fields.find((f) => f.field === name)?.message
      : undefined
  const set =
    (key: keyof FormState) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
      setForm({ ...form, [key]: e.target.value })
  const toggleTier = (tier: Tier) =>
    setForm({
      ...form,
      allowed_model_tiers: form.allowed_model_tiers.includes(tier)
        ? form.allowed_model_tiers.filter((t) => t !== tier)
        : [...form.allowed_model_tiers, tier],
    })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    setSaved(false)
    const limit = (value: string) => (value.trim() === '' ? undefined : Number(value))
    save.mutate(
      {
        default_model_tier: form.default_model_tier,
        allowed_model_tiers: form.allowed_model_tiers,
        user_daily_token_limit: limit(form.user_daily_token_limit),
        clear_user_daily_token_limit: form.user_daily_token_limit.trim() === '',
        org_monthly_token_limit: limit(form.org_monthly_token_limit),
        clear_org_monthly_token_limit: form.org_monthly_token_limit.trim() === '',
        max_concurrent_tasks: Number(form.max_concurrent_tasks),
        max_schedules: Number(form.max_schedules),
        retention_days: Number(form.retention_days),
      },
      { onSuccess: () => setSaved(true) },
    )
  }
  const defaultNotAllowed = !form.allowed_model_tiers.includes(form.default_model_tier)
  return (
    <>
      <PageHeader
        title="Settings"
        description="Models, limits and retention for this organization."
      />
      <form onSubmit={submit} className="space-y-6" aria-label="Organization settings">
        <ErrorNotice error={save.error} />
        {saved && (
          <p role="status" className="rounded-md bg-green-50 p-2 text-sm text-green-800">
            Settings saved.
          </p>
        )}
        <Card title="Models">
          <div className="grid gap-4 sm:grid-cols-2">
            <fieldset>
              <legend className="text-sm font-medium text-slate-800">Allowed tiers</legend>
              <div className="mt-2 space-y-1">
                {TIERS.map((tier) => (
                  <label key={tier.value} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={form.allowed_model_tiers.includes(tier.value)}
                      onChange={() => toggleTier(tier.value)}
                    />
                    {tier.label}
                  </label>
                ))}
              </div>
            </fieldset>
            <Field
              label="Default tier"
              htmlFor="default-tier"
              error={
                defaultNotAllowed
                  ? 'The default tier must be one of the allowed tiers.'
                  : fieldError('default_model_tier')
              }
            >
              <Select
                id="default-tier"
                value={form.default_model_tier}
                onChange={set('default_model_tier')}
              >
                {TIERS.map((tier) => (
                  <option key={tier.value} value={tier.value}>
                    {tier.label}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
        </Card>
        <Card title="Limits">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="Tokens per user per day"
              htmlFor="user-daily"
              hint="Empty for no limit"
              error={fieldError('user_daily_token_limit')}
            >
              <Input
                id="user-daily"
                type="number"
                min={1}
                value={form.user_daily_token_limit}
                onChange={set('user_daily_token_limit')}
              />
            </Field>
            <Field
              label="Tokens per org per month"
              htmlFor="org-monthly"
              hint="Empty for no limit"
              error={fieldError('org_monthly_token_limit')}
            >
              <Input
                id="org-monthly"
                type="number"
                min={1}
                value={form.org_monthly_token_limit}
                onChange={set('org_monthly_token_limit')}
              />
            </Field>
            <Field
              label="Concurrent tasks"
              htmlFor="concurrent"
              hint="1 to 1000"
              error={fieldError('max_concurrent_tasks')}
            >
              <Input
                id="concurrent"
                type="number"
                value={form.max_concurrent_tasks}
                onChange={set('max_concurrent_tasks')}
              />
            </Field>
            <Field
              label="Active schedules"
              htmlFor="schedules"
              hint="0 to 10000"
              error={fieldError('max_schedules')}
            >
              <Input
                id="schedules"
                type="number"
                value={form.max_schedules}
                onChange={set('max_schedules')}
              />
            </Field>
            <Field
              label="Retention (days)"
              htmlFor="retention"
              hint="1 to 3650"
              error={fieldError('retention_days')}
            >
              <Input
                id="retention"
                type="number"
                value={form.retention_days}
                onChange={set('retention_days')}
              />
            </Field>
          </div>
        </Card>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setForm(toForm(initial))}>
            Reset
          </Button>
          <Button
            type="submit"
            busy={save.isPending}
            disabled={defaultNotAllowed || form.allowed_model_tiers.length === 0}
          >
            Save settings
          </Button>
        </div>
      </form>
    </>
  )
}
