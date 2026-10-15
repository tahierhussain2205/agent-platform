export interface AuthUser {
  id: string
  email: string
  name: string
}

export interface AuthOrg {
  id: string
  name: string
  role: string
}

export interface AuthInvitation {
  id: string
  organizationName: string
  role: string
}

/** Dev only actions (the local Clerk stand-in); real Clerk renders its own components. */
export interface DevAuthActions {
  signIn(email: string, password: string): Promise<void>
  signUp(input: {
    email: string
    password: string
    firstName: string
    lastName: string
  }): Promise<void>
  createOrg(name: string): Promise<void>
  acceptInvitation(id: string): Promise<void>
  refresh(): Promise<void>
}

export interface AuthState {
  mode: 'clerk' | 'dev'
  status: 'loading' | 'signed_out' | 'signed_in'
  user: AuthUser | null
  orgs: AuthOrg[]
  activeOrgId: string | null
  invitations: AuthInvitation[]
  getToken(): Promise<string | null>
  setActiveOrg(orgId: string): Promise<void>
  signOut(): Promise<void>
  dev?: DevAuthActions
}
