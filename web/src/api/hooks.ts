import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback } from 'react'
import { useAuth } from '../auth/context'
import { ApiRequestError, apiFetch } from './client'

const LAGGING_MEMBERSHIP = ['not_a_member', 'membership_pending']

export type Privilege = 'admin' | 'user'
export type Tier = 'fast' | 'standard' | 'advanced'

export interface RoleRef {
  id: string
  title: string
  primary: boolean
}

export interface Me {
  user: { id: string; email: string; name: string | null }
  org: { id: string; name: string }
  membership: {
    id: string
    privilege: Privilege
    roles: { id: string; title: string; is_primary: boolean }[]
  }
}

export interface OrgOverview {
  org: {
    id: string
    name: string
    clerk_org_id: string
    status: string
    plan: string
    region: string
    created_at: string
  }
  counts: {
    members: number
    admins: number
    invited: number
    roles: number
    allowlisted_channels: number
    active_schedules: number
  }
  workspace: { name: string; external_id: string; status: string } | null
}

export interface Member {
  id: string
  email: string
  name: string | null
  privilege: Privilege
  status: 'active' | 'invited' | 'removed'
  joined_at: string | null
  created_at: string
  signed_up: boolean
  slack_linked: boolean
  roles: RoleRef[]
}

export interface Role {
  id: string
  title: string
  description: string
  status: 'active' | 'archived'
  members: number
  primary_for: number
  created_at: string
}

export interface OrgSettings {
  default_model_tier: Tier
  allowed_model_tiers: Tier[]
  user_daily_token_limit: number | null
  org_monthly_token_limit: number | null
  max_concurrent_tasks: number
  max_schedules: number
  retention_days: number
  display_settings: Record<string, unknown>
  updated_at: string
}

export interface AuditEntry {
  id: string
  occurred_at: string
  actor_type: 'user' | 'agent' | 'system'
  actor_email: string | null
  action: string
  target: string | null
  decision: 'allowed' | 'denied' | 'info'
  reason: string | null
  metadata: Record<string, unknown>
}

export interface SlackChannel {
  id: string
  external_id: string
  name: string | null
  is_private: boolean
  is_allowlisted: boolean
  bot_is_member: boolean
  archived: boolean
  members: number
  schedules: number
}

export interface SlackStatus {
  workspace: {
    id: string
    external_id: string
    name: string | null
    bot_user_id: string | null
    installed_at: string
  } | null
  channels: SlackChannel[]
  install_configured: boolean
}

export interface AccessRequest {
  id: string
  external_user_id: string
  email: string | null
  status: 'pending' | 'approved' | 'dismissed'
  created_at: string
  resolved_at: string | null
}

export type CredentialMode = 'org' | 'org_with_check' | 'user_required'

export interface IntegrationOperation {
  name: string
  description: string
  tier: 'read' | 'reversible_write' | 'irreversible'
  default_mode: CredentialMode
  mode: CredentialMode
  enabled: boolean
  overridden: boolean
}

export interface IntegrationConnection {
  id: string
  status: 'active' | 'expired'
  external_account_id: string | null
  created_at: string
  expires_at: string | null
  last_refreshed_at: string | null
}

export interface Integration {
  key: string
  title: string
  kind: 'native' | 'vendor_mcp' | 'custom_mcp'
  auth: 'slack' | 'oauth2' | 'github_app' | 'api_key' | 'none'
  description: string
  user_links: boolean
  enabled: boolean
  health: 'unknown' | 'ok' | 'degraded' | 'broken'
  approved: boolean
  configured: boolean
  connection: IntegrationConnection | null
  link: IntegrationConnection | null
  operations: IntegrationOperation[]
}

export type ScheduleKind = 'channel_summary' | 'jira_progress'

export interface Schedule {
  id: string
  name: string
  kind: ScheduleKind | 'other'
  channels: { id: string; name: string | null }[]
  project: string | null
  cron: string
  timezone: string
  description: string
  next_runs: string[]
  delivery: { type: 'dm' | 'channel'; channel_id: string | null; channel_name: string | null }
  status: 'active' | 'paused'
  pause_reason: 'owner_removed' | 'access_lost' | 'budget' | 'admin' | null
  pause_detail: string | null
  paused_at: string | null
  last_run_at: string | null
  created_at: string
  owner: { membership_id: string; email: string; name: string | null; status: string }
  can_manage: boolean
}

export interface ScheduleOptions {
  channels: { id: string; name: string; is_private: boolean }[]
  delivery_channels: { id: string; name: string }[]
  jira_enabled: boolean
  has_slack_account: boolean
  timezone: string
}

/** fetch bound to the current session token. */
export function useApi() {
  const { getToken } = useAuth()
  return useCallback(
    <T>(path: string, init?: RequestInit) => apiFetch<T>(getToken, path, init),
    [getToken],
  )
}

function useOrgKey() {
  return useAuth().activeOrgId ?? 'none'
}

export function useMe() {
  const api = useApi()
  const auth = useAuth()
  const org = useOrgKey()
  return useQuery({
    queryKey: [org, 'me'],
    queryFn: () => api<Me>('/api/me'),
    // Right after signing up or accepting an invitation, the identity provider's webhook may not
    // have reached the API yet: retry those answers for a few seconds before showing "no access".
    retry: (count, error) =>
      error instanceof ApiRequestError && LAGGING_MEMBERSHIP.includes(error.code) && count < 8,
    retryDelay: 1000,
    // Only once signed in with an active org; otherwise there is no token to send.
    enabled: auth.status === 'signed_in' && auth.activeOrgId !== null,
  })
}

export function useOrgOverview() {
  const api = useApi()
  const org = useOrgKey()
  return useQuery({ queryKey: [org, 'org'], queryFn: () => api<OrgOverview>('/api/org') })
}

export function useMembers() {
  const api = useApi()
  const org = useOrgKey()
  return useQuery({ queryKey: [org, 'members'], queryFn: () => api<Member[]>('/api/members') })
}

export function useRoles(includeArchived = false) {
  const api = useApi()
  const org = useOrgKey()
  return useQuery({
    queryKey: [org, 'roles', includeArchived],
    queryFn: () => api<Role[]>(`/api/roles${includeArchived ? '?include_archived=true' : ''}`),
  })
}

export function useSettings() {
  const api = useApi()
  const org = useOrgKey()
  return useQuery({ queryKey: [org, 'settings'], queryFn: () => api<OrgSettings>('/api/settings') })
}

export function useAudit(filters: {
  action?: string
  decision?: string
  actorType?: string
  before?: string
}) {
  const api = useApi()
  const org = useOrgKey()
  const params = new URLSearchParams({ limit: '25' })
  if (filters.action) params.set('action', filters.action)
  if (filters.decision) params.set('decision', filters.decision)
  if (filters.actorType) params.set('actor_type', filters.actorType)
  if (filters.before) params.set('before', filters.before)
  return useQuery({
    queryKey: [org, 'audit', params.toString()],
    queryFn: () =>
      api<{ items: AuditEntry[]; next_before: string | null }>(`/api/audit?${params.toString()}`),
  })
}

export function useSlack() {
  const api = useApi()
  const org = useOrgKey()
  return useQuery({ queryKey: [org, 'slack'], queryFn: () => api<SlackStatus>('/api/slack') })
}

export function useIntegrations() {
  const api = useApi()
  const org = useOrgKey()
  return useQuery({
    queryKey: [org, 'integrations'],
    queryFn: () => api<{ integrations: Integration[] }>('/api/integrations'),
  })
}

export function useSchedules() {
  const api = useApi()
  const org = useOrgKey()
  return useQuery({
    queryKey: [org, 'schedules'],
    queryFn: () =>
      api<{ schedules: Schedule[]; limits: { max_schedules: number; active: number } | null }>(
        '/api/schedules',
      ),
  })
}

export function useScheduleOptions(enabled = true) {
  const api = useApi()
  const org = useOrgKey()
  return useQuery({
    queryKey: [org, 'schedules', 'options'],
    queryFn: () => api<ScheduleOptions>('/api/schedules/options'),
    enabled,
  })
}

export function useAccessRequests(enabled: boolean) {
  const api = useApi()
  const org = useOrgKey()
  return useQuery({
    queryKey: [org, 'access-requests'],
    queryFn: () => api<AccessRequest[]>('/api/access-requests'),
    enabled,
  })
}

/** A mutation that refreshes the given query groups when it succeeds. */
export function useApiMutation<TInput, TOutput = unknown>(
  request: (api: ReturnType<typeof useApi>, input: TInput) => Promise<TOutput>,
  invalidate: string[],
) {
  const api = useApi()
  const client = useQueryClient()
  const org = useOrgKey()
  return useMutation({
    mutationFn: (input: TInput) => request(api, input),
    onSuccess: async () => {
      await Promise.all(invalidate.map((key) => client.invalidateQueries({ queryKey: [org, key] })))
    },
  })
}

export const json = (body: unknown): RequestInit => ({ body: JSON.stringify(body) })
