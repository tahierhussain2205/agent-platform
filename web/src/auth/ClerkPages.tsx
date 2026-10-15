import { CreateOrganization, OrganizationSwitcher, SignIn, SignUp } from '@clerk/react'

/** Clerk's hosted components for the routes the dev adapter implements itself. */
export function ClerkSignIn() {
  return <SignIn routing="hash" signUpUrl="/sign-up" />
}

export function ClerkSignUp() {
  return <SignUp routing="hash" signInUrl="/sign-in" />
}

export function ClerkCreateOrg() {
  return <CreateOrganization routing="hash" afterCreateOrganizationUrl="/" />
}

export function ClerkOrgSwitcher() {
  return <OrganizationSwitcher hidePersonal afterSelectOrganizationUrl="/" />
}
