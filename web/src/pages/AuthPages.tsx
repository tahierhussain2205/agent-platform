import { useState, type FormEvent, type ReactNode } from 'react'
import { Link, Navigate, useNavigate } from 'react-router'
import { useAuth } from '../auth/context'
import { Button, ErrorNotice, Field, Input } from '../components/ui'

/** Sign in, sign up, create org and invitations for the dev auth adapter (local Clerk stand-in). */

function AuthCard({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle?: string
  children: ReactNode
}) {
  return (
    <main className="flex min-h-full items-center justify-center px-4 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <p className="text-sm font-semibold text-indigo-600">Agent Platform</p>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight">{title}</h1>
          {subtitle && <p className="mt-1 text-sm text-slate-600">{subtitle}</p>}
        </div>
        <div className="rounded-lg bg-white p-6 shadow-sm ring-1 ring-slate-200">{children}</div>
      </div>
    </main>
  )
}

function useSubmit() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const run = async (action: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  return { busy, error, run, setError }
}

const DEV_ACCOUNTS = ['alice@acme.test', 'bob@acme.test', 'frank@both.test', 'dave@globex.test']

export function SignInPage() {
  const auth = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const { busy, error, run } = useSubmit()
  if (auth.status === 'signed_in') return <Navigate to="/" replace />

  const submit = (e: FormEvent) => {
    e.preventDefault()
    void run(async () => {
      await auth.dev!.signIn(email, password)
      navigate('/')
    })
  }
  return (
    <AuthCard title="Sign in" subtitle="Local development sign in (Clerk stand-in)">
      <form onSubmit={submit} className="space-y-4">
        <ErrorNotice error={error} />
        <Field label="Email" htmlFor="email">
          <Input
            id="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <Field label="Password" htmlFor="password">
          <Input
            id="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <Button type="submit" busy={busy} className="w-full">
          Sign in
        </Button>
      </form>
      <p className="mt-4 text-center text-sm text-slate-600">
        No account?{' '}
        <Link to="/sign-up" className="font-semibold text-indigo-600 hover:text-indigo-500">
          Sign up
        </Link>
      </p>
      <div className="mt-6 border-t border-slate-200 pt-4">
        <p className="text-xs font-medium text-slate-500">
          Seeded accounts (password: password123)
        </p>
        <div className="mt-2 flex flex-wrap gap-2">
          {DEV_ACCOUNTS.map((account) => (
            <button
              key={account}
              type="button"
              className="rounded bg-slate-100 px-2 py-1 text-xs text-slate-700 hover:bg-slate-200"
              onClick={() => {
                setEmail(account)
                setPassword('password123')
              }}
            >
              {account}
            </button>
          ))}
        </div>
      </div>
    </AuthCard>
  )
}

export function SignUpPage() {
  const auth = useAuth()
  const navigate = useNavigate()
  const [form, setForm] = useState({ firstName: '', lastName: '', email: '', password: '' })
  const { busy, error, run } = useSubmit()
  if (auth.status === 'signed_in') return <Navigate to="/" replace />
  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm({ ...form, [key]: e.target.value })
  const submit = (e: FormEvent) => {
    e.preventDefault()
    void run(async () => {
      await auth.dev!.signUp(form)
      navigate('/')
    })
  }
  return (
    <AuthCard title="Create your account" subtitle="Then set up your organization">
      <form onSubmit={submit} className="space-y-4">
        <ErrorNotice error={error} />
        <div className="grid grid-cols-2 gap-3">
          <Field label="First name" htmlFor="first-name">
            <Input
              id="first-name"
              autoComplete="given-name"
              value={form.firstName}
              onChange={set('firstName')}
            />
          </Field>
          <Field label="Last name" htmlFor="last-name">
            <Input
              id="last-name"
              autoComplete="family-name"
              value={form.lastName}
              onChange={set('lastName')}
            />
          </Field>
        </div>
        <Field label="Email" htmlFor="email">
          <Input
            id="email"
            type="email"
            autoComplete="email"
            required
            value={form.email}
            onChange={set('email')}
          />
        </Field>
        <Field label="Password" htmlFor="password" hint="At least 8 characters">
          <Input
            id="password"
            type="password"
            autoComplete="new-password"
            required
            minLength={8}
            value={form.password}
            onChange={set('password')}
          />
        </Field>
        <Button type="submit" busy={busy} className="w-full">
          Sign up
        </Button>
      </form>
      <p className="mt-4 text-center text-sm text-slate-600">
        Already have an account?{' '}
        <Link to="/sign-in" className="font-semibold text-indigo-600 hover:text-indigo-500">
          Sign in
        </Link>
      </p>
    </AuthCard>
  )
}

export function CreateOrgPage() {
  const auth = useAuth()
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const { busy, error, run } = useSubmit()
  if (auth.status === 'signed_out') return <Navigate to="/sign-in" replace />
  const submit = (e: FormEvent) => {
    e.preventDefault()
    void run(async () => {
      await auth.dev!.createOrg(name)
      navigate('/')
    })
  }
  return (
    <AuthCard
      title="Set up your organization"
      subtitle="You will be its admin. You can invite people next."
    >
      {auth.invitations.length > 0 && (
        <p className="mb-4 rounded-md bg-indigo-50 p-3 text-sm text-indigo-800">
          You have {auth.invitations.length} pending invitation
          {auth.invitations.length > 1 ? 's' : ''}.{' '}
          <Link to="/invitations" className="font-semibold underline">
            Review invitations
          </Link>
        </p>
      )}
      <form onSubmit={submit} className="space-y-4">
        <ErrorNotice error={error} />
        <Field label="Organization name" htmlFor="org-name">
          <Input
            id="org-name"
            required
            maxLength={200}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        <Button type="submit" busy={busy} className="w-full">
          Create organization
        </Button>
      </form>
    </AuthCard>
  )
}

export function InvitationsPage() {
  const auth = useAuth()
  const navigate = useNavigate()
  const { busy, error, run } = useSubmit()
  if (auth.status === 'signed_out') return <Navigate to="/sign-in" replace />
  return (
    <AuthCard title="Invitations" subtitle={auth.user?.email}>
      <ErrorNotice error={error} />
      {auth.invitations.length === 0 ? (
        <p className="text-sm text-slate-600">No pending invitations.</p>
      ) : (
        <ul className="divide-y divide-slate-200">
          {auth.invitations.map((invitation) => (
            <li key={invitation.id} className="flex items-center justify-between py-3">
              <div>
                <p className="text-sm font-medium">{invitation.organizationName}</p>
                <p className="text-xs text-slate-500">
                  as {invitation.role === 'org:admin' ? 'admin' : 'member'}
                </p>
              </div>
              <Button
                busy={busy}
                onClick={() =>
                  void run(async () => {
                    await auth.dev!.acceptInvitation(invitation.id)
                    navigate('/')
                  })
                }
              >
                Accept
              </Button>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-4 flex justify-between text-sm">
        <Link to="/create-org" className="text-indigo-600 hover:text-indigo-500">
          Create an organization instead
        </Link>
        <button
          type="button"
          className="text-slate-600 hover:text-slate-900"
          onClick={() => void auth.signOut()}
        >
          Sign out
        </button>
      </div>
    </AuthCard>
  )
}
