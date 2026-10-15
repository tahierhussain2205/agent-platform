import { config } from '../config'

/** An error response from the API: {"error": {"code", "message", "request_id", "fields"}}. */
export class ApiRequestError extends Error {
  status: number
  code: string
  requestId: string | null
  fields: { field: string; message: string }[]

  constructor(
    status: number,
    code: string,
    message: string,
    requestId: string | null,
    fields: { field: string; message: string }[] = [],
  ) {
    super(message)
    this.name = 'ApiRequestError'
    this.status = status
    this.code = code
    this.requestId = requestId
    this.fields = fields
  }
}

export type TokenGetter = () => Promise<string | null>

export async function apiFetch<T>(
  getToken: TokenGetter,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const token = await getToken()
  const headers = new Headers(init.headers)
  if (token) headers.set('authorization', `Bearer ${token}`)
  if (init.body !== undefined && !headers.has('content-type'))
    headers.set('content-type', 'application/json')
  let response: Response
  try {
    response = await fetch(config.apiUrl + path, { ...init, headers })
  } catch {
    throw new ApiRequestError(
      0,
      'network',
      'Could not reach the server. Check your connection and try again.',
      null,
    )
  }
  const requestId = response.headers.get('x-request-id')
  if (response.status === 204) return undefined as T
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const error = (
      body as {
        error?: { code?: string; message?: string; fields?: { field: string; message: string }[] }
      } | null
    )?.error
    throw new ApiRequestError(
      response.status,
      error?.code ?? 'error',
      error?.message ?? `Request failed (${response.status}).`,
      requestId,
      error?.fields ?? [],
    )
  }
  return body as T
}
