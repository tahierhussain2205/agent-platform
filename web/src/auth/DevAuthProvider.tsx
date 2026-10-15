import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { config } from '../config'
import { AuthContext } from './context'
import type { AuthInvitation, AuthOrg, AuthState, AuthUser } from './types'

/**
 * Auth against the local Clerk stand-in (fakes service). Same session token format as Clerk
 * (RS256 JWT v2); the API verifies it exactly as it would a real Clerk token.
 */

const SESSION_KEY = 'ap.dev.session'

interface DevSession {
  session_id: string
  status: 'active' | 'pending'
  active_org_id: string | null
  user: { id: string; email: string; first_name: string; last_name: string }
  organizations: { id: string; name: string; slug: string; role: string }[]
  invitations: { id: string; organization_name: string; role: string }[]
}

class DevAuthError extends Error {}

function readSession(): string | null {
  try {
    return localStorage.getItem(SESSION_KEY)
  } catch {
    return null
  }
}

function writeSession(id: string | null): void {
  try {
    if (id) localStorage.setItem(SESSION_KEY, id)
    else localStorage.removeItem(SESSION_KEY)
  } catch {
    // Storage blocked: the session lives only in memory for this tab.
  }
}

async function call<T>(path: string, init: RequestInit = {}, session?: string | null): Promise<T> {
  const headers = new Headers(init.headers)
  if (session) headers.set('authorization', `Bearer ${session}`)
  if (init.body !== undefined) headers.set('content-type', 'application/json')
  const response = await fetch(config.devAuthUrl + path, { ...init, headers })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const message = (body as { errors?: { long_message?: string; message?: string }[] } | null)
      ?.errors?.[0]
    throw new DevAuthError(
      message?.long_message ?? message?.message ?? `Sign in failed (${response.status}).`,
    )
  }
  return body as T
}

export function DevAuthProvider({ children }: { children: ReactNode }) {
  const [sessionId, setSessionId] = useState<string | null>(() => readSession())
  const [session, setSession] = useState<DevSession | null>(null)
  const [loading, setLoading] = useState(sessionId !== null)
  const tokenCache = useRef<{ jwt: string; exp: number; org: string | null } | null>(null)

  const adopt = useCallback((state: DevSession | null) => {
    tokenCache.current = null
    setSession(state)
    setSessionId(state?.session_id ?? null)
    writeSession(state?.session_id ?? null)
  }, [])

  const refresh = useCallback(async () => {
    const id = readSession()
    if (!id) {
      adopt(null)
      return
    }
    try {
      adopt(await call<DevSession>('/dev/session', {}, id))
    } catch {
      adopt(null) // expired or ended elsewhere
    }
  }, [adopt])

  useEffect(() => {
    if (!sessionId) return
    let cancelled = false
    call<DevSession>('/dev/session', {}, sessionId)
      .then((state) => !cancelled && setSession(state))
      .catch(() => !cancelled && adopt(null))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
    // Only on mount: later changes go through adopt().
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const getToken = useCallback(async (): Promise<string | null> => {
    if (!sessionId) return null
    const cached = tokenCache.current
    const now = Date.now() / 1000
    if (cached && cached.exp - 10 > now && cached.org === (session?.active_org_id ?? null))
      return cached.jwt
    const { jwt } = await call<{ jwt: string }>(
      '/dev/session/tokens',
      { method: 'POST' },
      sessionId,
    )
    const payload = JSON.parse(atob(jwt.split('.')[1]!.replace(/-/g, '+').replace(/_/g, '/'))) as {
      exp: number
    }
    tokenCache.current = { jwt, exp: payload.exp, org: session?.active_org_id ?? null }
    return jwt
  }, [sessionId, session?.active_org_id])

  const value = useMemo<AuthState>(() => {
    const user: AuthUser | null = session
      ? {
          id: session.user.id,
          email: session.user.email,
          name:
            [session.user.first_name, session.user.last_name].filter(Boolean).join(' ') ||
            session.user.email,
        }
      : null
    const orgs: AuthOrg[] = (session?.organizations ?? []).map((o) => ({
      id: o.id,
      name: o.name,
      role: o.role,
    }))
    const invitations: AuthInvitation[] = (session?.invitations ?? []).map((i) => ({
      id: i.id,
      organizationName: i.organization_name,
      role: i.role,
    }))
    return {
      mode: 'dev',
      status: loading ? 'loading' : session ? 'signed_in' : 'signed_out',
      user,
      orgs,
      activeOrgId: session?.active_org_id ?? null,
      invitations,
      getToken,
      async setActiveOrg(orgId) {
        adopt(
          await call<DevSession>(
            '/dev/session/active-org',
            { method: 'POST', body: JSON.stringify({ organization_id: orgId }) },
            sessionId,
          ),
        )
      },
      async signOut() {
        if (sessionId)
          await call('/dev/session/end', { method: 'POST' }, sessionId).catch(() => undefined)
        adopt(null)
      },
      dev: {
        async signIn(email, password) {
          adopt(
            await call<DevSession>('/dev/sign-in', {
              method: 'POST',
              body: JSON.stringify({ email, password }),
            }),
          )
        },
        async signUp({ email, password, firstName, lastName }) {
          adopt(
            await call<DevSession>('/dev/sign-up', {
              method: 'POST',
              body: JSON.stringify({ email, password, first_name: firstName, last_name: lastName }),
            }),
          )
        },
        async createOrg(name) {
          adopt(
            await call<DevSession>(
              '/dev/organizations',
              { method: 'POST', body: JSON.stringify({ name }) },
              sessionId,
            ),
          )
        },
        async acceptInvitation(id) {
          adopt(
            await call<DevSession>(`/dev/invitations/${id}/accept`, { method: 'POST' }, sessionId),
          )
        },
        refresh,
      },
    }
  }, [session, loading, getToken, adopt, sessionId, refresh])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
