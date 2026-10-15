import { createContext, useContext } from 'react'
import type { Me } from '../api/hooks'

export const MeContext = createContext<Me | null>(null)

/** The signed in member (from our API: privilege comes from the database, not the token). */
export function useMember(): Me {
  const me = useContext(MeContext)
  if (!me) throw new Error('useMember must be used inside the console layout')
  return me
}
