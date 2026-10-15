import { execSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

/** Back to the seed: Clerk stand-in data (keys kept) and the seeded database rows. */
export default async function globalSetup() {
  const fakes = process.env.FAKES_URL ?? 'http://localhost:4100'
  const response = await fetch(`${fakes}/_admin/reset`, { method: 'POST' })
  if (!response.ok) throw new Error(`fakes reset failed: ${response.status}`)
  const root = fileURLToPath(new URL('..', import.meta.url))
  execSync('docker compose exec -T api app seed', { cwd: root, stdio: 'inherit' })
}
