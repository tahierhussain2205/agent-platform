import { useState } from 'react'
import { useAudit, type AuditEntry } from '../api/hooks'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNotice,
  Input,
  PageHeader,
  Select,
  Spinner,
} from '../components/ui'

const DECISION_TONE = { allowed: 'green', denied: 'red', info: 'slate' } as const

export function AuditPage() {
  const [action, setAction] = useState('')
  const [decision, setDecision] = useState('')
  const [actorType, setActorType] = useState('')
  const [cursors, setCursors] = useState<string[]>([])
  const before = cursors.at(-1)
  const audit = useAudit({
    action: action.trim() || undefined,
    decision: decision || undefined,
    actorType: actorType || undefined,
    before,
  })
  const resetPaging = () => setCursors([])
  return (
    <>
      <PageHeader
        title="Audit log"
        description="Every console change, identity sync and agent decision for this organization."
      />
      <Card>
        <div className="mb-4 flex flex-wrap gap-3">
          <Input
            aria-label="Filter by action"
            placeholder="Action starts with (e.g. member.)"
            className="max-w-64"
            value={action}
            onChange={(e) => (setAction(e.target.value), resetPaging())}
          />
          <Select
            aria-label="Filter by decision"
            className="max-w-40"
            value={decision}
            onChange={(e) => (setDecision(e.target.value), resetPaging())}
          >
            <option value="">Any decision</option>
            <option value="allowed">Allowed</option>
            <option value="denied">Denied</option>
            <option value="info">Info</option>
          </Select>
          <Select
            aria-label="Filter by actor"
            className="max-w-40"
            value={actorType}
            onChange={(e) => (setActorType(e.target.value), resetPaging())}
          >
            <option value="">Any actor</option>
            <option value="user">People</option>
            <option value="agent">Agent</option>
            <option value="system">System</option>
          </Select>
        </div>
        {audit.isPending ? (
          <Spinner />
        ) : audit.error ? (
          <ErrorNotice error={audit.error} />
        ) : audit.data.items.length === 0 ? (
          <EmptyState>No matching entries.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-slate-500">
                <tr>
                  <th className="pb-2 pr-3 font-medium">When</th>
                  <th className="pb-2 pr-3 font-medium">Actor</th>
                  <th className="pb-2 pr-3 font-medium">Action</th>
                  <th className="pb-2 pr-3 font-medium">Target</th>
                  <th className="pb-2 font-medium">Decision</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200">
                {audit.data.items.map((entry) => (
                  <tr key={entry.id} data-testid="audit-row">
                    <td className="whitespace-nowrap py-2 pr-3 text-slate-600">
                      {new Date(entry.occurred_at).toLocaleString()}
                    </td>
                    <td className="py-2 pr-3">{actorLabel(entry)}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{entry.action}</td>
                    <td className="py-2 pr-3 text-slate-700">
                      {entry.target ?? ''}
                      {entry.reason && <div className="text-xs text-slate-500">{entry.reason}</div>}
                    </td>
                    <td className="py-2">
                      <Badge tone={DECISION_TONE[entry.decision]}>{entry.decision}</Badge>
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
            disabled={!audit.data?.next_before}
            onClick={() =>
              audit.data?.next_before && setCursors([...cursors, audit.data.next_before])
            }
          >
            Older
          </Button>
        </div>
      </Card>
    </>
  )
}

/** Agent actions are recorded on behalf of the person who asked. */
function actorLabel(entry: AuditEntry): string {
  if (entry.actor_type === 'agent') {
    return entry.actor_email ? `Agent for ${entry.actor_email}` : 'Agent'
  }
  return entry.actor_email ?? entry.actor_type
}
