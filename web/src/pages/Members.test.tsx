import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ConsoleGate } from '../components/Layout'
import { mockFetch, renderRoutes } from '../test/render'
import { MembersPage } from './Members'

const me = {
  user: { id: 'u', email: 'alice@acme.test', name: 'Alice' },
  org: { id: 'o', name: 'Acme Corp' },
  membership: { id: 'm-alice', privilege: 'admin', roles: [] },
}
const quinn = {
  id: 'm-quinn',
  email: 'quinn@acme.test',
  name: 'Quinn',
  privilege: 'user',
  status: 'active',
  joined_at: null,
  created_at: '',
  signed_up: true,
  slack_linked: false,
  roles: [{ id: 'r-qa', title: 'QA', primary: true }],
}
const roles = ['QA', 'Engineer', 'Manager'].map((title) => ({
  id: `r-${title.toLowerCase()}`,
  title,
  description: '',
  status: 'active',
  members: 0,
  primary_for: 0,
  created_at: '',
}))

afterEach(() => vi.unstubAllGlobals())

describe('Members page', () => {
  it('assigns roles with exactly one primary', async () => {
    const saved: unknown[] = []
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me }),
        'GET /api/members': () => ({ body: [quinn] }),
        'GET /api/roles': () => ({ body: roles }),
        'PUT /api/members/m-quinn/roles': (init) => {
          saved.push(JSON.parse(String(init!.body)))
          return { body: quinn }
        },
      }).fn,
    )
    const user = userEvent.setup()
    renderRoutes([
      {
        path: '/',
        element: <ConsoleGate />,
        children: [{ index: true, element: <MembersPage /> }],
      },
    ])
    const row = await screen.findByTestId('member-quinn@acme.test')
    await user.click(within(row).getByRole('button', { name: 'Edit roles' }))
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByLabelText('QA')).toBeChecked()
    expect(within(dialog).getByLabelText('Primary: QA')).toBeChecked()
    expect(within(dialog).getByLabelText('Primary: Engineer')).toBeDisabled()

    await user.click(within(dialog).getByLabelText('Engineer'))
    await user.click(within(dialog).getByLabelText('Primary: Engineer'))
    await user.click(within(dialog).getByLabelText('QA')) // unchecking a non primary role keeps the primary
    expect(within(dialog).getByLabelText('Primary: Engineer')).toBeChecked()
    await user.click(within(dialog).getByRole('button', { name: 'Save roles' }))
    await waitFor(() =>
      expect(saved).toEqual([{ role_ids: ['r-engineer'], primary_role_id: 'r-engineer' }]),
    )
  })

  it('moves the primary when the primary role is unchecked, and clears it when none remain', async () => {
    const saved: unknown[] = []
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me }),
        'GET /api/members': () => ({ body: [quinn] }),
        'GET /api/roles': () => ({ body: roles }),
        'PUT /api/members/m-quinn/roles': (init) => {
          saved.push(JSON.parse(String(init!.body)))
          return { body: quinn }
        },
      }).fn,
    )
    const user = userEvent.setup()
    renderRoutes([
      {
        path: '/',
        element: <ConsoleGate />,
        children: [{ index: true, element: <MembersPage /> }],
      },
    ])
    await user.click(
      within(await screen.findByTestId('member-quinn@acme.test')).getByRole('button', {
        name: 'Edit roles',
      }),
    )
    const dialog = await screen.findByRole('dialog')
    await user.click(await within(dialog).findByLabelText('Manager'))
    await user.click(within(dialog).getByLabelText('QA')) // the primary goes away
    expect(within(dialog).getByLabelText('Primary: Manager')).toBeChecked()
    await user.click(within(dialog).getByLabelText('Manager'))
    await user.click(within(dialog).getByRole('button', { name: 'Save roles' }))
    await waitFor(() => expect(saved).toEqual([{ role_ids: [], primary_role_id: null }]))
  })

  it('shows API errors next to the member with the request id', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        'GET /api/me': () => ({ body: me }),
        'GET /api/members': () => ({
          body: [{ ...quinn, id: 'm-alice', email: 'alice@acme.test', privilege: 'admin' }],
        }),
        'PATCH /api/members/m-alice': () => ({
          status: 409,
          headers: { 'x-request-id': 'req-77' },
          body: {
            error: {
              code: 'last_admin',
              message: 'An organization must keep at least one active admin.',
            },
          },
        }),
      }).fn,
    )
    const user = userEvent.setup()
    renderRoutes([
      {
        path: '/',
        element: <ConsoleGate />,
        children: [{ index: true, element: <MembersPage /> }],
      },
    ])
    const row = await screen.findByTestId('member-alice@acme.test')
    expect(within(row).getByText('(you)')).toBeInTheDocument()
    await user.click(within(row).getByRole('button', { name: 'Make member' }))
    expect(
      await within(row).findByText('An organization must keep at least one active admin.'),
    ).toBeInTheDocument()
    expect(within(row).getByText('Request ID: req-77')).toBeInTheDocument()
  })
})
