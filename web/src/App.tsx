import { lazy, Suspense, type ReactNode } from 'react'
import { createBrowserRouter, Link, RouterProvider } from 'react-router'
import { useAuth } from './auth/context'
import { AdminOnly, ConsoleGate } from './components/Layout'
import { ThemeSwitcher } from './components/ThemeSwitcher'
import { Spinner } from './components/ui'
import { AuditPage } from './pages/Audit'
import { CatalogPage } from './pages/Catalog'
import { CreateOrgPage, InvitationsPage, SignInPage, SignUpPage } from './pages/AuthPages'
import { IntegrationsPage } from './pages/Integrations'
import { MembersPage } from './pages/Members'
import { OverviewPage } from './pages/Overview'
import { RepositoriesPage } from './pages/Repositories'
import { RolesPage } from './pages/Roles'
import { SchedulesPage } from './pages/Schedules'
import { SettingsPage } from './pages/Settings'
import { SlackPage } from './pages/Slack'
import { UsagePage } from './pages/Usage'

const clerkPages = () => import('./auth/ClerkPages')
const ClerkSignIn = lazy(() => clerkPages().then((m) => ({ default: m.ClerkSignIn })))
const ClerkSignUp = lazy(() => clerkPages().then((m) => ({ default: m.ClerkSignUp })))
const ClerkCreateOrg = lazy(() => clerkPages().then((m) => ({ default: m.ClerkCreateOrg })))

/** Dev routes render our pages; with real Clerk, Clerk's components. */
function ByMode({ dev, clerk }: { dev: ReactNode; clerk: ReactNode }) {
  const { mode } = useAuth()
  if (mode === 'dev') return <>{dev}</>
  return (
    <Suspense fallback={<Spinner />}>
      <div className="relative flex min-h-full items-center justify-center p-8">
        <div className="absolute right-4 top-4">
          <ThemeSwitcher />
        </div>
        {clerk}
      </div>
    </Suspense>
  )
}

function NotFound() {
  return (
    <div className="flex min-h-full flex-col items-center justify-center gap-2 p-8">
      <h1 className="text-lg font-semibold">Page not found</h1>
      <Link
        to="/"
        className="text-sm font-medium text-fg underline decoration-line-strong underline-offset-4 hover:decoration-fg"
      >
        Back to the console
      </Link>
    </div>
  )
}

const router = createBrowserRouter([
  { path: '/sign-in', element: <ByMode dev={<SignInPage />} clerk={<ClerkSignIn />} /> },
  { path: '/sign-up', element: <ByMode dev={<SignUpPage />} clerk={<ClerkSignUp />} /> },
  { path: '/create-org', element: <ByMode dev={<CreateOrgPage />} clerk={<ClerkCreateOrg />} /> },
  { path: '/invitations', element: <InvitationsPage /> },
  {
    path: '/',
    element: <ConsoleGate />,
    children: [
      { index: true, element: <OverviewPage /> },
      { path: 'members', element: <MembersPage /> },
      { path: 'roles', element: <RolesPage /> },
      { path: 'slack', element: <SlackPage /> },
      { path: 'integrations', element: <IntegrationsPage /> },
      { path: 'catalog', element: <CatalogPage /> },
      { path: 'repositories', element: <RepositoriesPage /> },
      { path: 'schedules', element: <SchedulesPage /> },
      {
        path: 'settings',
        element: (
          <AdminOnly>
            <SettingsPage />
          </AdminOnly>
        ),
      },
      {
        path: 'usage',
        element: (
          <AdminOnly>
            <UsagePage />
          </AdminOnly>
        ),
      },
      {
        path: 'audit',
        element: (
          <AdminOnly>
            <AuditPage />
          </AdminOnly>
        ),
      },
    ],
  },
  { path: '*', element: <NotFound /> },
])

export default function App() {
  return <RouterProvider router={router} />
}
