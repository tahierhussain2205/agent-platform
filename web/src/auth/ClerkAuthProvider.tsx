import {
  ClerkProvider,
  useAuth as useClerkAuth,
  useClerk,
  useOrganization,
  useUser,
} from '@clerk/react'
import { useMemo, type ReactNode } from 'react'
import { config } from '../config'
import { AuthContext } from './context'
import type { AuthOrg, AuthState } from './types'

/**
 * Real Clerk (Core 3). Loaded only when VITE_CLERK_PUBLISHABLE_KEY is set; sign in, sign up and
 * organization management use Clerk's own components (see ClerkPages.tsx).
 *
 * The orgs come from the user's own memberships (`user.organizationMemberships`), which Clerk loads
 * with the session. `useOrganizationList` fetches separately and starts empty: the console gate read
 * that as "no org yet" and sent people who had one back to Create organization, again and again.
 * While Clerk is loading the user, the state is loading; the active org always counts as one of
 * theirs, even before the memberships catch up with an org just created.
 */
export function ClerkBridge({ children }: { children: ReactNode }) {
  const { isLoaded, isSignedIn, getToken, orgId, signOut } = useClerkAuth()
  const { isLoaded: userLoaded, user } = useUser()
  const { organization } = useOrganization()
  const clerk = useClerk()

  const value = useMemo<AuthState>(() => {
    const orgs: AuthOrg[] = (user?.organizationMemberships ?? []).map((m) => ({
      id: m.organization.id,
      name: m.organization.name,
      role: m.role,
    }))
    if (orgId && !orgs.some((o) => o.id === orgId)) {
      orgs.push({ id: orgId, name: organization?.name ?? 'Organization', role: '' })
    }
    const loading = !isLoaded || (isSignedIn === true && !userLoaded)
    return {
      mode: 'clerk',
      status: loading ? 'loading' : isSignedIn ? 'signed_in' : 'signed_out',
      user: user
        ? {
            id: user.id,
            email: user.primaryEmailAddress?.emailAddress ?? '',
            name: user.fullName ?? user.primaryEmailAddress?.emailAddress ?? '',
          }
        : null,
      orgs,
      activeOrgId: orgId ?? null,
      invitations: [],
      getToken: () => getToken(),
      async setActiveOrg(org) {
        await clerk.setActive({ organization: org })
      },
      async signOut() {
        await signOut()
      },
    }
  }, [isLoaded, isSignedIn, userLoaded, getToken, orgId, organization?.name, signOut, user, clerk])
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export default function ClerkAuthProvider({ children }: { children: ReactNode }) {
  return (
    <ClerkProvider publishableKey={config.clerkPublishableKey!}>
      <ClerkBridge>{children}</ClerkBridge>
    </ClerkProvider>
  )
}
