import { useState } from 'react'
import { json, useApiMutation, useAutonomy, type Autonomy } from '../api/hooks'
import { Card, ErrorNotice, Spinner } from './ui'

const CHOICES: { value: Autonomy['org']; title: string; detail: string }[] = [
  {
    value: 'ask',
    title: 'Ask before every change',
    detail:
      'The agent asks in the thread before it creates a ticket, moves its status, pushes a branch or opens a pull request.',
  },
  {
    value: 'auto',
    title: 'Let people choose Auto',
    detail:
      'People who switch to Auto in the agent’s Home tab in Slack let it make reversible changes without asking. Irreversible actions, protected paths and large plans still ask.',
  },
]

/** The org's autonomy ceiling (spec 4.5): saved as soon as it is chosen. */
export function AutonomyCard({ isAdmin }: { isAdmin: boolean }) {
  const autonomy = useAutonomy()
  const save = useApiMutation(
    (api, org: Autonomy['org']) =>
      api<Autonomy>('/api/autonomy', { method: 'PUT', ...json({ org }) }),
    ['autonomy'],
  )
  // The requested choice while saving (the mutation reports pending a tick late).
  const [requested, setRequested] = useState<Autonomy['org'] | null>(null)
  if (autonomy.isPending) return <Spinner />
  if (autonomy.error) return <ErrorNotice error={autonomy.error} />
  const current = requested ?? autonomy.data.org
  return (
    <Card title="Autonomy">
      <fieldset disabled={!isAdmin || requested !== null} className="space-y-3">
        <legend className="sr-only">How far the agent may go without asking</legend>
        {CHOICES.map((choice) => (
          <label key={choice.value} className="flex items-start gap-3 text-sm">
            <input
              type="radio"
              name="autonomy"
              className="mt-1"
              value={choice.value}
              checked={current === choice.value}
              onChange={() => {
                setRequested(choice.value)
                save.mutate(choice.value, { onSettled: () => setRequested(null) })
              }}
            />
            <span>
              <span className="font-medium text-fg">{choice.title}</span>
              <span className="block text-fg-2">{choice.detail}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <ErrorNotice error={save.error} />
      {save.isSuccess && requested === null && (
        <p role="status" className="mt-3 text-sm text-ok">
          Saved.
        </p>
      )}
    </Card>
  )
}
