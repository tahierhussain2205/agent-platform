import { lazy, Suspense, type ReactNode } from 'react'
import { authMode } from '../config'
import { DevAuthProvider } from './DevAuthProvider'

const ClerkAuthProvider = lazy(() => import('./ClerkAuthProvider'))

/** Real Clerk when a publishable key is configured; otherwise the local Clerk stand-in. */
export function AuthProvider({ children }: { children: ReactNode }) {
  if (authMode === 'clerk') {
    return (
      <Suspense fallback={null}>
        <ClerkAuthProvider>{children}</ClerkAuthProvider>
      </Suspense>
    )
  }
  return <DevAuthProvider>{children}</DevAuthProvider>
}
