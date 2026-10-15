import { ClerkProvider, useAuth as useClerkAuth, useOrganizationList, useUser } from '@clerk/react'
import { useMemo, type ReactNode } from 'react'
import { config } from '../config'
import { AuthContext } from './context'
import type { AuthState } from './types'

/**
 * Real Clerk (Core 3). Loaded only when VITE_CLERK_PUBLISHABLE_KEY is set; sign in, sign up and
 * organization management use Clerk's own components (see ClerkPages.tsx).
 * Not exercised locally (no Clerk instance yet, blocker B-003).
 */
function ClerkBridge({ children }: { children: ReactNode }) {
  const { isLoaded, isSignedIn, getToken, orgId, signOut } = useClerkAuth()
  const { user } = useUser()
  const { userMemberships, setActive } = useOrganizationList({
    userMemberships: { infinite: true },
  })

  const value = useMemo<AuthState>(
    () => ({
      mode: 'clerk',
      status: !isLoaded ? 'loading' : isSignedIn ? 'signed_in' : 'signed_out',
      user: user
        ? {
            id: user.id,
            email: user.primaryEmailAddress?.emailAddress ?? '',
            name: user.fullName ?? user.primaryEmailAddress?.emailAddress ?? '',
          }
        : null,
      orgs: (userMemberships?.data ?? []).map((m) => ({
        id: m.organization.id,
        name: m.organization.name,
        role: m.role,
      })),
      activeOrgId: orgId ?? null,
      invitations: [],
      getToken: () => getToken(),
      async setActiveOrg(organization) {
        await setActive?.({ organization })
      },
      async signOut() {
        await signOut()
      },
    }),
    [isLoaded, isSignedIn, getToken, orgId, signOut, user, userMemberships?.data, setActive],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export default function ClerkAuthProvider({ children }: { children: ReactNode }) {
  return (
    <ClerkProvider publishableKey={config.clerkPublishableKey!}>
      <ClerkBridge>{children}</ClerkBridge>
    </ClerkProvider>
  )
}
