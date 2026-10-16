import {
  useEffect,
  useRef,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react'
import { ApiRequestError } from '../api/client'
import { useAuth } from '../auth/context'

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost'

const variants: Record<Variant, string> = {
  primary:
    'bg-accent text-accent-fg hover:bg-accent-hover focus-visible:outline-fg disabled:opacity-40',
  secondary:
    'bg-surface text-fg ring-1 ring-inset ring-line-strong hover:bg-muted disabled:text-fg-4 disabled:hover:bg-surface',
  danger:
    'bg-red-600 text-white hover:bg-red-500 focus-visible:outline-red-600 disabled:opacity-50',
  ghost: 'text-fg-2 hover:bg-muted hover:text-fg disabled:text-fg-4 disabled:hover:bg-transparent',
}

export function Button({
  variant = 'primary',
  className = '',
  busy = false,
  children,
  disabled,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; busy?: boolean }) {
  return (
    <button
      type="button"
      {...props}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={`inline-flex items-center justify-center gap-2 rounded-md px-3 py-1.5 text-sm font-semibold shadow-sm focus-visible:outline-2 focus-visible:outline-offset-2 disabled:cursor-not-allowed ${variants[variant]} ${className}`}
    >
      {busy && <Spinner className="h-3.5 w-3.5" />}
      {children}
    </button>
  )
}

export function Spinner({ className = 'h-5 w-5' }: { className?: string }) {
  return (
    <span
      role="status"
      aria-label="Loading"
      className={`inline-block animate-spin rounded-full border-2 border-current border-r-transparent ${className}`}
    />
  )
}

const tones = {
  neutral: 'bg-muted text-fg-2 ring-line-strong',
  strong: 'bg-accent text-accent-fg ring-accent',
  green: 'bg-ok-soft text-ok ring-ok-line',
  amber: 'bg-warn-soft text-warn ring-warn-line',
  red: 'bg-bad-soft text-bad ring-bad-line',
}

export function Badge({
  tone = 'neutral',
  children,
}: {
  tone?: keyof typeof tones
  children: ReactNode
}) {
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${tones[tone]}`}
    >
      {children}
    </span>
  )
}

export function Field({
  label,
  hint,
  error,
  children,
  htmlFor,
}: {
  label: string
  hint?: string
  error?: string
  children: ReactNode
  htmlFor: string
}) {
  return (
    <div className="space-y-1">
      <label htmlFor={htmlFor} className="block text-sm font-medium text-fg">
        {label}
      </label>
      {children}
      {hint && !error && <p className="text-xs text-fg-3">{hint}</p>}
      {error && (
        <p className="text-xs text-bad" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}

const inputClass =
  'block w-full rounded-md border-0 bg-surface px-2.5 py-1.5 text-sm text-fg shadow-sm ring-1 ring-inset ring-line-strong placeholder:text-fg-4 focus:ring-2 focus:ring-inset focus:ring-fg disabled:bg-muted disabled:text-fg-3'

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={`${inputClass} ${props.className ?? ''}`} />
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={`${inputClass} ${props.className ?? ''}`} />
}

export function TextArea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={`${inputClass} ${props.className ?? ''}`} />
}

export function Card({
  title,
  actions,
  children,
}: {
  title?: string
  actions?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="rounded-lg bg-surface shadow-sm ring-1 ring-line">
      {(title || actions) && (
        <header className="flex items-center justify-between border-b border-line px-4 py-3">
          {title && <h2 className="text-sm font-semibold text-fg">{title}</h2>}
          {actions}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: string
  actions?: ReactNode
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-fg">{title}</h1>
        {description && <p className="mt-1 text-sm text-fg-2">{description}</p>}
      </div>
      {actions}
    </div>
  )
}

/** Shows an API (or other) error; includes the request id so support can find it in the logs.
 * When the session is over (401), it offers to sign in again instead of leaving a dead end. */
export function ErrorNotice({ error, onDismiss }: { error: unknown; onDismiss?: () => void }) {
  const auth = useAuth()
  if (!error) return null
  const message = error instanceof Error ? error.message : 'Something went wrong.'
  const requestId = error instanceof ApiRequestError ? error.requestId : null
  const signedOut = error instanceof ApiRequestError && error.status === 401
  return (
    <div
      role="alert"
      className="flex items-start justify-between gap-4 rounded-md bg-bad-soft p-3 text-sm text-bad ring-1 ring-bad-line"
    >
      <div>
        <p className="font-medium">{message}</p>
        {requestId && <p className="mt-0.5 text-xs text-bad/80">Request ID: {requestId}</p>}
        {signedOut && (
          <button
            type="button"
            onClick={() => void auth.signOut()}
            className="mt-2 font-semibold underline underline-offset-4"
          >
            Sign in again
          </button>
        )}
      </div>
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          className="text-bad hover:text-fg"
          aria-label="Dismiss"
        >
          ×
        </button>
      )}
    </div>
  )
}

export function Dialog({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
}) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      aria-labelledby="dialog-title"
      className="m-auto w-full max-w-lg rounded-lg bg-surface p-0 text-fg shadow-xl ring-1 ring-line backdrop:bg-black/60"
    >
      <div className="border-b border-line px-5 py-3">
        <h2 id="dialog-title" className="text-base font-semibold">
          {title}
        </h2>
      </div>
      <div className="p-5">{open && children}</div>
    </dialog>
  )
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-sm text-fg-3">{children}</p>
}
