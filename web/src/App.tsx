import { lazy, Suspense, type ReactNode } from 'react'
import { createBrowserRouter, Link, RouterProvider } from 'react-router'
import { useAuth } from './auth/context'
import { AdminOnly, ConsoleGate } from './components/Layout'
import { Spinner } from './components/ui'
import { AuditPage } from './pages/Audit'
import { CreateOrgPage, InvitationsPage, SignInPage, SignUpPage } from './pages/AuthPages'
import { IntegrationsPage } from './pages/Integrations'
import { MembersPage } from './pages/Members'
import { OverviewPage } from './pages/Overview'
import { RolesPage } from './pages/Roles'
import { SettingsPage } from './pages/Settings'
import { SlackPage } from './pages/Slack'

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
      <div className="flex min-h-full items-center justify-center p-8">{clerk}</div>
    </Suspense>
  )
}

function NotFound() {
  return (
    <div className="flex min-h-full flex-col items-center justify-center gap-2 p-8">
      <h1 className="text-lg font-semibold">Page not found</h1>
      <Link to="/" className="text-sm text-indigo-600 hover:text-indigo-500">
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
      {
        path: 'settings',
        element: (
          <AdminOnly>
            <SettingsPage />
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
