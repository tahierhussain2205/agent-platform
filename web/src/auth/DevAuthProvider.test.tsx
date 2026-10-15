import { act, render, screen, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mockFetch } from '../test/render'
import { useAuth } from './context'
import { DevAuthProvider } from './DevAuthProvider'
import type { AuthState } from './types'

const session = (orgId: string | null) => ({
  session_id: 'sess_1',
  status: orgId ? 'active' : 'pending',
  active_org_id: orgId,
  user: { id: 'user_frank', email: 'frank@both.test', first_name: 'Frank', last_name: 'Twoorgs' },
  organizations: [
    { id: 'org_acme', name: 'Acme Corp', slug: 'acme', role: 'org:member' },
    { id: 'org_globex', name: 'Globex Inc', slug: 'globex', role: 'org:member' },
  ],
  invitations: [],
})
const jwt = (exp: number) => `h.${btoa(JSON.stringify({ exp }))}.s`

let auth: AuthState
const capture = (value: AuthState) => {
  auth = value
}
function Probe() {
  const value = useAuth()
  useEffect(() => capture(value))
  return (
    <p data-testid="status">
      {value.status}:{value.activeOrgId ?? 'none'}
    </p>
  )
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.unstubAllGlobals())

describe('DevAuthProvider', () => {
  it('signs in, caches tokens per org until expiry, and signs out', async () => {
    let active = 'org_acme'
    const { fn, calls } = mockFetch({
      [`POST /clerk/dev/sign-in`]: () => ({ body: session(active) }),
      [`POST /clerk/dev/session/tokens`]: () => ({
        body: { jwt: jwt(Math.floor(Date.now() / 1000) + 60) },
      }),
      [`POST /clerk/dev/session/active-org`]: (init) => {
        active = JSON.parse(String(init!.body)).organization_id
        return { body: session(active) }
      },
      [`POST /clerk/dev/session/end`]: () => ({ body: { status: 'ended' } }),
    })
    vi.stubGlobal('fetch', fn)
    render(
      <DevAuthProvider>
        <Probe />
      </DevAuthProvider>,
    )
    expect(screen.getByTestId('status')).toHaveTextContent('signed_out')

    await act(() => auth.dev!.signIn('frank@both.test', 'password123'))
    expect(screen.getByTestId('status')).toHaveTextContent('signed_in:org_acme')
    expect(localStorage.getItem('ap.dev.session')).toBe('sess_1')

    await act(async () => {
      await auth.getToken()
      await auth.getToken()
    })
    expect(calls.filter((c) => c.url.endsWith('/session/tokens'))).toHaveLength(1)

    await act(() => auth.setActiveOrg('org_globex'))
    expect(screen.getByTestId('status')).toHaveTextContent('signed_in:org_globex')
    await act(async () => void (await auth.getToken()))
    expect(calls.filter((c) => c.url.endsWith('/session/tokens'))).toHaveLength(2) // a new org needs a new token

    await act(() => auth.signOut())
    expect(screen.getByTestId('status')).toHaveTextContent('signed_out')
    expect(localStorage.getItem('ap.dev.session')).toBeNull()
  })

  it('refreshes tokens that are about to expire', async () => {
    const { fn, calls } = mockFetch({
      'POST /clerk/dev/sign-in': () => ({ body: session('org_acme') }),
      'POST /clerk/dev/session/tokens': () => ({
        body: { jwt: jwt(Math.floor(Date.now() / 1000) + 5) },
      }),
    })
    vi.stubGlobal('fetch', fn)
    render(
      <DevAuthProvider>
        <Probe />
      </DevAuthProvider>,
    )
    await act(() => auth.dev!.signIn('a@b.test', 'x'))
    await act(async () => {
      await auth.getToken()
      await auth.getToken()
    })
    expect(calls.filter((c) => c.url.endsWith('/session/tokens'))).toHaveLength(2)
  })

  it('restores a stored session and drops it when the stand-in no longer knows it', async () => {
    localStorage.setItem('ap.dev.session', 'sess_gone')
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /clerk/dev/session': () => ({
          status: 401,
          body: { errors: [{ message: 'Sign in again' }] },
        }),
      }).fn,
    )
    render(
      <DevAuthProvider>
        <Probe />
      </DevAuthProvider>,
    )
    expect(screen.getByTestId('status')).toHaveTextContent('loading')
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('signed_out'))
    expect(localStorage.getItem('ap.dev.session')).toBeNull()
  })

  it('surfaces sign in errors from the stand-in', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'POST /clerk/dev/sign-in': () => ({
          status: 422,
          body: { errors: [{ long_message: 'Incorrect email or password' }] },
        }),
      }).fn,
    )
    render(
      <DevAuthProvider>
        <Probe />
      </DevAuthProvider>,
    )
    await expect(auth.dev!.signIn('a@b.test', 'bad')).rejects.toThrow('Incorrect email or password')
  })

  it('keeps working when storage is blocked', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.stubGlobal(
      'fetch',
      mockFetch({ 'POST /clerk/dev/sign-in': () => ({ body: session('org_acme') }) }).fn,
    )
    render(
      <DevAuthProvider>
        <Probe />
      </DevAuthProvider>,
    )
    await act(() => auth.dev!.signIn('a@b.test', 'x'))
    expect(screen.getByTestId('status')).toHaveTextContent('signed_in:org_acme')
    vi.restoreAllMocks()
  })
})
