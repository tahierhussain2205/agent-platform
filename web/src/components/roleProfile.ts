import type {
  Role,
  RoleDetail,
  RoleFocus,
  RoleFormat,
  RoleOptions,
  RoleProfile,
  RoleStyle,
} from '../api/hooks'

export const DETAIL: Record<RoleDetail, string> = {
  brief: 'Brief',
  balanced: 'Balanced',
  thorough: 'Thorough',
}
export const STYLE: Record<RoleStyle, string> = {
  plain: 'Plain language',
  technical: 'Technical',
  business: 'Business impact',
}
export const FORMAT: Record<RoleFormat, string> = {
  bullets: 'Bullets',
  paragraphs: 'Paragraphs',
  summary_first: 'Summary first',
}
export const FOCUS: Record<RoleFocus, string> = {
  risks: 'Risks',
  next_steps: 'Next steps',
  owners: 'Owners',
  dates: 'Dates',
  code: 'Code',
  customer_impact: 'Customer impact',
  metrics: 'Metrics',
}

/** The profile as short labels, for role cards. */
export function profileLabels(profile: RoleProfile | undefined): string[] {
  if (!profile) return []
  const labels = [
    profile.detail && DETAIL[profile.detail],
    profile.style && STYLE[profile.style],
    profile.format && FORMAT[profile.format],
  ].filter((l): l is string => Boolean(l))
  if (profile.focus?.length)
    labels.push(`Calls out ${profile.focus.map((f) => FOCUS[f].toLowerCase()).join(', ')}`)
  return labels
}

const INTEGRATIONS: Record<string, string> = {
  slack: 'Slack',
  jira: 'Jira',
  github: 'GitHub',
  google_cloud_logging: 'Cloud logs',
}

export const integrationName = (key: string) => INTEGRATIONS[key] ?? key

/** "jira.create_issue" as "Jira: create issue". */
export function operationLabel(key: string): string {
  const [integration = '', operation = ''] = key.split('.', 2)
  return `${integrationName(integration)}: ${operation.replaceAll('_', ' ')}`
}

/** The limits as short labels, for role cards (fields an older API leaves out count as unset). */
export function limitLabels(role: Partial<Role>, options?: RoleOptions): string[] {
  const title = (key: string) => options?.playbooks.find((p) => p.key === key)?.title ?? key
  const labels: string[] = []
  if (role.allowed_playbooks)
    labels.push(
      role.allowed_playbooks.length
        ? `Only: ${role.allowed_playbooks.map(title).join(', ')}`
        : 'No playbooks',
    )
  if (role.max_mode === 'ask') labels.push('Always Ask mode')
  const list = (prefix: string, keys: string[] | undefined) => {
    if (keys?.length) labels.push(`${prefix}: ${keys.map(operationLabel).join(', ')}`)
  }
  list('Never', role.blocked_operations)
  list('Asks before', role.ask_operations)
  list('Approves', role.approves_operations)
  return labels
}
