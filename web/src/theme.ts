import { useCallback, useEffect, useSyncExternalStore } from 'react'

/** The viewer's theme: light, dark, or whatever their system prefers (the default). */
export type ThemeChoice = 'light' | 'dark' | 'system'

const KEY = 'console-theme'
const DARK = '(prefers-color-scheme: dark)'
const listeners = new Set<() => void>()

function stored(): ThemeChoice {
  try {
    const value = localStorage.getItem(KEY)
    return value === 'light' || value === 'dark' ? value : 'system'
  } catch {
    return 'system'
  }
}

function systemDark(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia(DARK).matches
}

export function resolve(choice: ThemeChoice): 'light' | 'dark' {
  return choice === 'system' ? (systemDark() ? 'dark' : 'light') : choice
}

/** Puts the theme on <html> (the `dark` class drives every dark style). */
export function applyTheme(choice: ThemeChoice = stored()): void {
  document.documentElement.classList.toggle('dark', resolve(choice) === 'dark')
}

export function setThemeChoice(choice: ThemeChoice): void {
  try {
    if (choice === 'system') localStorage.removeItem(KEY)
    else localStorage.setItem(KEY, choice)
  } catch {
    // Storage blocked (private mode): the choice lasts for this page only.
  }
  current = choice
  applyTheme(choice)
  listeners.forEach((notify) => notify())
}

let current: ThemeChoice = stored()

function subscribe(notify: () => void): () => void {
  listeners.add(notify)
  const media = typeof window.matchMedia === 'function' ? window.matchMedia(DARK) : null
  const onSystemChange = () => {
    if (current === 'system') {
      applyTheme('system')
      notify()
    }
  }
  media?.addEventListener('change', onSystemChange)
  return () => {
    listeners.delete(notify)
    media?.removeEventListener('change', onSystemChange)
  }
}

/** The current choice and the theme it resolves to, kept in step with the system's preference. */
export function useTheme(): {
  choice: ThemeChoice
  resolved: 'light' | 'dark'
  setChoice: (choice: ThemeChoice) => void
} {
  const choice = useSyncExternalStore(subscribe, () => current)
  const resolved = useSyncExternalStore(subscribe, () => resolve(current))
  useEffect(() => applyTheme(choice), [choice])
  const setChoice = useCallback((next: ThemeChoice) => setThemeChoice(next), [])
  return { choice, resolved, setChoice }
}
