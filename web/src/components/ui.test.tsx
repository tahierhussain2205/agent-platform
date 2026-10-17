import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiRequestError } from '../api/client'
import { fakeAuth, renderUi } from '../test/render'
import { setThemeChoice } from '../theme'
import { ThemeSwitcher } from './ThemeSwitcher'
import { ErrorNotice } from './ui'

afterEach(() => {
  setThemeChoice('system')
  vi.unstubAllGlobals()
})

describe('ErrorNotice', () => {
  it('offers to sign in again when the session is over', async () => {
    const signOut = vi.fn(async () => undefined)
    renderUi(
      <ErrorNotice
        error={new ApiRequestError(401, 'token_expired', 'The session has expired.', 'req-1')}
      />,
      fakeAuth({ signOut }),
    )
    expect(await screen.findByText('The session has expired.')).toBeInTheDocument()
    expect(screen.getByText('Request ID: req-1')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Sign in again' }))
    expect(signOut).toHaveBeenCalledOnce()
  })

  it('shows other errors without it', async () => {
    renderUi(<ErrorNotice error={new ApiRequestError(409, 'conflict', 'Already there.', null)} />)
    expect(await screen.findByText('Already there.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in again' })).not.toBeInTheDocument()
  })
})

describe('ThemeSwitcher', () => {
  it('switches the theme on the page and remembers the choice', async () => {
    renderUi(<ThemeSwitcher />)
    const html = document.documentElement
    const dark = await screen.findByRole('button', { name: 'Dark theme' })
    await userEvent.click(dark)
    expect(html).toHaveClass('dark')
    expect(dark).toHaveAttribute('aria-pressed', 'true')
    expect(localStorage.getItem('console-theme')).toBe('dark')

    await userEvent.click(screen.getByRole('button', { name: 'Light theme' }))
    expect(html).not.toHaveClass('dark')
    expect(localStorage.getItem('console-theme')).toBe('light')

    await userEvent.click(screen.getByRole('button', { name: 'Use the system theme' }))
    expect(localStorage.getItem('console-theme')).toBeNull()
    expect(screen.getByRole('button', { name: 'Use the system theme' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('follows the system when nothing is chosen', async () => {
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: query.includes('dark'),
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }))
    setThemeChoice('system')
    expect(document.documentElement).toHaveClass('dark')
    renderUi(<ThemeSwitcher />)
    expect(await screen.findByRole('button', { name: 'Use the system theme' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })
})
