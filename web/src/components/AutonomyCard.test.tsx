import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mockFetch, renderUi } from '../test/render'
import { AutonomyCard } from './AutonomyCard'

afterEach(() => vi.unstubAllGlobals())

describe('AutonomyCard', () => {
  it('saves the ceiling as soon as it is chosen', async () => {
    let org = 'ask'
    const fetch = mockFetch({
      'GET /api/autonomy': () => ({ body: { org, set: org !== 'ask' } }),
      'PUT /api/autonomy': (init) => {
        org = String(JSON.parse(String(init?.body)).org)
        return { body: { org, set: true } }
      },
    })
    vi.stubGlobal('fetch', fetch.fn)
    renderUi(<AutonomyCard isAdmin />)
    expect(await screen.findByRole('radio', { name: /Ask before every change/ })).toBeChecked()
    await userEvent.click(screen.getByRole('radio', { name: /Let people choose Auto/ }))
    expect(await screen.findByText('Saved.')).toBeInTheDocument()
    expect(org).toBe('auto')
  })

  it('is read only for members', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({ 'GET /api/autonomy': () => ({ body: { org: 'auto', set: true } }) }).fn,
    )
    renderUi(<AutonomyCard isAdmin={false} />)
    const auto = await screen.findByRole('radio', { name: /Let people choose Auto/ })
    expect(auto).toBeChecked()
    expect(auto).toBeDisabled()
  })
})
