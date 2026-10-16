import { lazy, Suspense, useState, type ReactNode } from 'react'
import { Navigate, NavLink, Outlet } from 'react-router'
import { ApiRequestError } from '../api/client'
import { useMe } from '../api/hooks'
import { useAuth } from '../auth/context'
import { MeContext, useMember } from './member'
import { Badge, Button, ErrorNotice, Spinner } from './ui'

const ClerkOrgSwitcher = lazy(() =>
  import('../auth/ClerkPages').then((m) => ({ default: m.ClerkOrgSwitcher })),
)

function FullPage({ children }: { children: ReactNode }) {
  return <div className="flex min-h-full items-center justify-center p-8">{children}</div>
}

const NO_ACCESS: Record<string, string> = {
  not_a_member: 'You are not a member of this organization.',
  membership_pending: 'Your invitation has not been accepted yet.',
  org_inactive: 'This organization is not active.',
  user_disabled: 'Your account is disabled.',
  session_pending: 'Finish setting up your account first.',
}

function NoAccess({ error }: { error: ApiRequestError }) {
  const auth = useAuth()
  const others = auth.orgs.filter((o) => o.id !== auth.activeOrgId)
  return (
    <FullPage>
      <div className="max-w-md rounded-lg bg-white p-6 text-center shadow-sm ring-1 ring-slate-200">
        <h1 className="text-lg font-semibold">No access</h1>
        <p className="mt-2 text-sm text-slate-600">{NO_ACCESS[error.code] ?? error.message}</p>
        <div className="mt-4 flex flex-wrap justify-center gap-2">
          {others.map((org) => (
            <Button key={org.id} variant="secondary" onClick={() => void auth.setActiveOrg(org.id)}>
              Switch to {org.name}
            </Button>
          ))}
          <Button variant="ghost" onClick={() => void auth.signOut()}>
            Sign out
          </Button>
        </div>
      </div>
    </FullPage>
  )
}

/** Signed in, with an active org, and a member of it according to our API. */
export function ConsoleGate() {
  const auth = useAuth()
  const me = useMe()
  if (auth.status === 'loading') {
    return (
      <FullPage>
        <Spinner />
      </FullPage>
    )
  }
  if (auth.status === 'signed_out') return <Navigate to="/sign-in" replace />
  if (auth.orgs.length === 0)
    return <Navigate to={auth.invitations.length > 0 ? '/invitations' : '/create-org'} replace />
  if (me.isPending) {
    return (
      <FullPage>
        <Spinner />
      </FullPage>
    )
  }
  if (me.error) {
    if (me.error instanceof ApiRequestError && me.error.status === 403)
      return <NoAccess error={me.error} />
    return (
      <FullPage>
        <div className="w-full max-w-md space-y-3">
          <ErrorNotice error={me.error} />
          <Button variant="secondary" onClick={() => void me.refetch()}>
            Try again
          </Button>
        </div>
      </FullPage>
    )
  }
  return (
    <MeContext.Provider value={me.data}>
      <Shell />
    </MeContext.Provider>
  )
}

function OrgSwitcher() {
  const auth = useAuth()
  const [busy, setBusy] = useState(false)
  if (auth.mode === 'clerk') {
    return (
      <Suspense fallback={null}>
        <ClerkOrgSwitcher />
      </Suspense>
    )
  }
  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="sr-only">Organization</span>
      <select
        aria-label="Organization"
        className="rounded-md border-0 bg-slate-800 py-1 pl-2 pr-8 text-sm font-medium text-white ring-1 ring-slate-700 focus:ring-2 focus:ring-indigo-400"
        value={auth.activeOrgId ?? ''}
        disabled={busy}
        onChange={async (e) => {
          setBusy(true)
          try {
            await auth.setActiveOrg(e.target.value)
          } finally {
            setBusy(false)
          }
        }}
      >
        {auth.orgs.map((org) => (
          <option key={org.id} value={org.id}>
            {org.name}
          </option>
        ))}
      </select>
    </label>
  )
}

function Shell() {
  const auth = useAuth()
  const me = useMember()
  const isAdmin = me.membership.privilege === 'admin'
  const nav = [
    { to: '/', label: 'Overview', end: true },
    { to: '/members', label: 'Members' },
    { to: '/roles', label: 'Roles' },
    { to: '/slack', label: 'Slack' },
    { to: '/integrations', label: 'Integrations' },
    { to: '/catalog', label: 'Catalog' },
    { to: '/schedules', label: 'Schedules' },
    ...(isAdmin
      ? [
          { to: '/settings', label: 'Settings' },
          { to: '/audit', label: 'Audit log' },
        ]
      : []),
  ]
  return (
    <div className="flex min-h-full">
      <aside className="hidden w-56 shrink-0 bg-slate-900 text-slate-300 md:block">
        <div className="px-4 py-4 text-sm font-semibold text-white">Agent Platform</div>
        <nav aria-label="Main" className="space-y-1 px-2">
          {nav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `block rounded-md px-3 py-2 text-sm font-medium ${isActive ? 'bg-slate-800 text-white' : 'hover:bg-slate-800 hover:text-white'}`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between gap-4 bg-slate-900 px-4 py-2.5 md:bg-white md:ring-1 md:ring-slate-200">
          <div className="flex items-center gap-3 rounded-md md:bg-slate-900 md:px-2 md:py-1">
            <OrgSwitcher />
          </div>
          <div className="flex items-center gap-3 text-sm">
            <span className="hidden text-slate-700 sm:inline" data-testid="current-user">
              {auth.user?.email}
            </span>
            <Badge tone={isAdmin ? 'indigo' : 'slate'}>{isAdmin ? 'Admin' : 'Member'}</Badge>
            <Button variant="secondary" onClick={() => void auth.signOut()}>
              Sign out
            </Button>
          </div>
        </header>
        <nav
          aria-label="Mobile"
          className="flex gap-1 overflow-x-auto border-b border-slate-200 bg-white px-2 py-1 md:hidden"
        >
          {nav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `whitespace-nowrap rounded px-2 py-1 text-sm ${isActive ? 'bg-slate-100 font-semibold' : ''}`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 sm:px-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}

export function AdminOnly({ children }: { children: ReactNode }) {
  const me = useMember()
  if (me.membership.privilege !== 'admin') return <Navigate to="/" replace />
  return <>{children}</>
}
