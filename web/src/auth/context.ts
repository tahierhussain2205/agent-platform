import { createContext, useContext } from 'react'
import type { AuthState } from './types'

export const AuthContext = createContext<AuthState | null>(null)

export function useAuth(): AuthState {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth must be used inside an auth provider')
  return value
}
