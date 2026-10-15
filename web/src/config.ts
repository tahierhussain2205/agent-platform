/** Build time configuration (Vite env). */
export const config = {
  apiUrl: (import.meta.env.VITE_API_URL as string | undefined) ?? 'http://localhost:8000',
  clerkPublishableKey: import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as string | undefined,
  devAuthUrl:
    (import.meta.env.VITE_DEV_AUTH_URL as string | undefined) ?? 'http://localhost:4100/clerk',
}

export type AuthMode = 'clerk' | 'dev'

/** Real Clerk when a publishable key is configured, otherwise the local Clerk stand-in. */
export const authMode: AuthMode = config.clerkPublishableKey ? 'clerk' : 'dev'
