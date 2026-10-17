import type { RoleDetail, RoleFocus, RoleFormat, RoleProfile, RoleStyle } from '../api/hooks'

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
