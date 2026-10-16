import { CreateOrganization, OrganizationSwitcher, SignIn, SignUp } from '@clerk/react'
import { Link } from 'react-router'
import { useAuth } from './context'

/** Clerk's hosted components for the routes the dev adapter implements itself. */
export function ClerkSignIn() {
  return <SignIn routing="hash" signUpUrl="/sign-up" />
}

export function ClerkSignUp() {
  return <SignUp routing="hash" signInUrl="/sign-in" />
}

export function ClerkCreateOrg() {
  const [first, ...others] = useAuth().orgs
  return (
    <div className="flex flex-col items-center gap-4">
      <CreateOrganization routing="hash" afterCreateOrganizationUrl="/" />
      {first && (
        <Link to="/" className="text-sm text-indigo-600 hover:text-indigo-500">
          You already belong to {others.length ? 'an organization' : first.name}: back to the
          console
        </Link>
      )}
    </div>
  )
}

export function ClerkOrgSwitcher() {
  return <OrganizationSwitcher hidePersonal afterSelectOrganizationUrl="/" />
}
