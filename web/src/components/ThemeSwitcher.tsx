import type { ReactNode } from 'react'
import { useTheme, type ThemeChoice } from '../theme'

const icon = (path: ReactNode) => (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
    className="h-4 w-4"
    aria-hidden="true"
  >
    {path}
  </svg>
)

const CHOICES: { value: ThemeChoice; label: string; icon: ReactNode }[] = [
  {
    value: 'light',
    label: 'Light theme',
    icon: icon(
      <>
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
      </>,
    ),
  },
  {
    value: 'dark',
    label: 'Dark theme',
    icon: icon(<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z" />),
  },
  {
    value: 'system',
    label: 'Use the system theme',
    icon: icon(
      <>
        <rect x="3" y="4" width="18" height="12" rx="2" />
        <path d="M8 20h8M12 16v4" />
      </>,
    ),
  },
]

/** Light, dark or the system's theme; remembered in this browser. */
export function ThemeSwitcher() {
  const { choice, setChoice } = useTheme()
  return (
    <div
      role="group"
      aria-label="Theme"
      className="inline-flex rounded-md bg-muted p-0.5 ring-1 ring-inset ring-line"
    >
      {CHOICES.map((c) => (
        <button
          key={c.value}
          type="button"
          aria-label={c.label}
          aria-pressed={choice === c.value}
          title={c.label}
          onClick={() => setChoice(c.value)}
          className={`rounded p-1.5 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-fg ${
            choice === c.value
              ? 'bg-surface text-fg shadow-sm ring-1 ring-line-strong'
              : 'text-fg-3 hover:text-fg'
          }`}
        >
          {c.icon}
        </button>
      ))}
    </div>
  )
}
