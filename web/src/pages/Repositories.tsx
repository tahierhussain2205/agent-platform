import { useState, type FormEvent } from 'react'
import {
  json,
  useApiMutation,
  useRepositories,
  type RecipeStatus,
  type Repository,
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
  Spinner,
  TextArea,
} from '../components/ui'

const STATUS: Record<RecipeStatus, { label: string; tone: 'green' | 'amber' | 'red' | 'neutral' }> =
  {
    works: { label: 'Tests ran', tone: 'green' },
    failed: { label: 'Tests failed last time', tone: 'amber' },
    unavailable: { label: 'Could not run', tone: 'red' },
    untested: { label: 'Not run yet', tone: 'neutral' },
  }

function Recipe({ repo }: { repo: Repository }) {
  const { test, source, status, last_run } = repo.recipe
  if (!test)
    return (
      <p className="text-sm text-fg-2">
        No test command yet: it is detected from the repository the first time the agent changes it.
      </p>
    )
  const shown = STATUS[status ?? 'untested']
  return (
    <div className="space-y-1">
      <p className="text-sm text-fg-2">
        Tests: <code className="rounded bg-muted px-1 py-0.5 text-xs">{test}</code>{' '}
        <Badge tone={shown.tone}>{shown.label}</Badge>{' '}
        <Badge>{source === 'admin' ? 'Set by an admin' : 'Detected'}</Badge>
      </p>
      {last_run?.summary && (
        <p className="text-xs text-fg-3">
          Last run{last_run.at ? ` ${new Date(last_run.at).toLocaleString()}` : ''}:{' '}
          {last_run.summary}
        </p>
      )}
    </div>
  )
}

function RepositoryForm({ repo, onDone }: { repo: Repository; onDone: () => void }) {
  const [paths, setPaths] = useState(repo.protected_paths.join('\n'))
  const [command, setCommand] = useState(
    repo.recipe.source === 'admin' ? (repo.recipe.test ?? '') : '',
  )
  const save = useApiMutation<{ protected_paths: string[]; test_command: string }>(
    (api, body) => api(`/api/repositories/${repo.id}`, { method: 'PATCH', ...json(body) }),
    ['repositories'],
  )
  function submit(event: FormEvent) {
    event.preventDefault()
    save.mutate(
      {
        protected_paths: paths
          .split('\n')
          .map((p) => p.trim())
          .filter(Boolean),
        test_command: command.trim(),
      },
      { onSuccess: onDone },
    )
  }
  return (
    <form onSubmit={submit} className="space-y-4">
      <ErrorNotice error={save.error} />
      <Field
        label="Protected paths"
        htmlFor="protected-paths"
        hint="One path or pattern per line, like migrations/ or *.tf. A change touching one needs a person's approval, even in Auto."
      >
        <TextArea
          id="protected-paths"
          rows={5}
          value={paths}
          onChange={(e) => setPaths(e.target.value)}
        />
      </Field>
      <Field
        label="Test command"
        htmlFor="test-command"
        hint="How the tests run in the sandbox (no network). Leave empty to detect it from the repository."
      >
        <Input
          id="test-command"
          value={command}
          placeholder={
            repo.recipe.source === 'detected' ? (repo.recipe.test ?? '') : 'python -m pytest -q'
          }
          onChange={(e) => setCommand(e.target.value)}
        />
      </Field>
      <div className="flex justify-end gap-2">
        <Button type="button" variant="secondary" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" busy={save.isPending}>
          Save
        </Button>
      </div>
    </form>
  )
}

export function RepositoriesPage() {
  const repositories = useRepositories()
  const [editing, setEditing] = useState<Repository | null>(null)
  if (repositories.isPending) return <Spinner />
  if (repositories.error) return <ErrorNotice error={repositories.error} />
  const { can_manage: canManage } = repositories.data
  const repos = repositories.data.repositories
  return (
    <div className="space-y-6">
      <PageHeader
        title="Repositories"
        description="Where the agent may change code: the paths that always need a person's approval, and how each repository's tests run in a sandbox."
      />
      <Card>
        {repos.length === 0 ? (
          <EmptyState>No repositories yet. Propose services in the catalog to add them.</EmptyState>
        ) : (
          <ul className="divide-y divide-line" aria-label="Repositories">
            {repos.map((repo) => (
              <li key={repo.id} aria-label={repo.full_name} className="space-y-2 px-4 py-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="space-y-1">
                    <p className="font-medium text-fg">
                      {repo.full_name}{' '}
                      <span className="text-xs text-fg-3">{repo.default_branch}</span>
                    </p>
                    {repo.services.length > 0 && (
                      <p className="text-xs text-fg-2">Services: {repo.services.join(', ')}</p>
                    )}
                    <p className="text-sm text-fg-2">
                      Protected paths:{' '}
                      {repo.protected_paths.length === 0 ? (
                        <span className="text-fg-3">none (CI workflows are always protected)</span>
                      ) : (
                        repo.protected_paths.map((p) => (
                          <code key={p} className="mr-1 rounded bg-muted px-1 py-0.5 text-xs">
                            {p}
                          </code>
                        ))
                      )}
                    </p>
                    <Recipe repo={repo} />
                  </div>
                  {canManage && (
                    <Button variant="secondary" onClick={() => setEditing(repo)}>
                      Edit
                    </Button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Dialog
        open={editing !== null}
        title={`Edit ${editing?.full_name ?? ''}`}
        onClose={() => setEditing(null)}
      >
        {editing && (
          <RepositoryForm key={editing.id} repo={editing} onDone={() => setEditing(null)} />
        )}
      </Dialog>
    </div>
  )
}
