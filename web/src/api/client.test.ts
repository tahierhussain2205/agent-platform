import { afterEach, describe, expect, it, vi } from 'vitest'
import { mockFetch } from '../test/render'
import { ApiRequestError, apiFetch } from './client'

afterEach(() => vi.unstubAllGlobals())

describe('apiFetch', () => {
  it('sends the bearer token and JSON content type', async () => {
    const { fn, calls } = mockFetch({
      'POST /api/roles': () => ({ status: 201, body: { id: 'r1' } }),
    })
    vi.stubGlobal('fetch', fn)
    const result = await apiFetch<{ id: string }>(async () => 'tok', '/api/roles', {
      method: 'POST',
      body: JSON.stringify({ title: 'x' }),
    })
    expect(result).toEqual({ id: 'r1' })
    const headers = new Headers(calls[0]!.init!.headers)
    expect(headers.get('authorization')).toBe('Bearer tok')
    expect(headers.get('content-type')).toBe('application/json')
  })

  it('omits the authorization header without a token', async () => {
    const { fn, calls } = mockFetch({ 'GET /api/orgs': () => ({ body: [] }) })
    vi.stubGlobal('fetch', fn)
    await apiFetch(async () => null, '/api/orgs')
    expect(new Headers(calls[0]!.init!.headers).has('authorization')).toBe(false)
  })

  it('turns error bodies into ApiRequestError with code, request id and fields', async () => {
    const { fn } = mockFetch({
      'PATCH /api/settings': () => ({
        status: 422,
        headers: { 'x-request-id': 'req-123' },
        body: {
          error: {
            code: 'invalid',
            message: 'Too small',
            fields: [{ field: 'retention_days', message: 'Too small' }],
          },
        },
      }),
    })
    vi.stubGlobal('fetch', fn)
    const error = await apiFetch(async () => 't', '/api/settings', {
      method: 'PATCH',
      body: '{}',
    }).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiRequestError)
    expect(error).toMatchObject({
      status: 422,
      code: 'invalid',
      message: 'Too small',
      requestId: 'req-123',
      fields: [{ field: 'retention_days', message: 'Too small' }],
    })
  })

  it('handles non JSON errors and network failures', async () => {
    vi.stubGlobal('fetch', async () => new Response('oops', { status: 502 }))
    await expect(apiFetch(async () => 't', '/api/me')).rejects.toMatchObject({
      status: 502,
      code: 'error',
    })
    vi.stubGlobal('fetch', async () => {
      throw new TypeError('Failed to fetch')
    })
    await expect(apiFetch(async () => 't', '/api/me')).rejects.toMatchObject({
      status: 0,
      code: 'network',
    })
  })

  it('returns undefined for 204', async () => {
    const { fn } = mockFetch({ 'DELETE /api/members/m1': () => ({ status: 204 }) })
    vi.stubGlobal('fetch', fn)
    await expect(
      apiFetch(async () => 't', '/api/members/m1', { method: 'DELETE' }),
    ).resolves.toBeUndefined()
  })
})
