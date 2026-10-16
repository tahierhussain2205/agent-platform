/** Cadences in the console: presets (weekdays, daily, weekly at a time) or a raw cron expression. */

export type Preset = 'weekdays' | 'daily' | 'weekly' | 'custom'

export interface Cadence {
  preset: Preset
  time: string // HH:MM
  day: string // 0-6, for weekly
  cron: string // for custom
}

export function toCron(c: Cadence): string {
  const [h = '9', m = '0'] = c.time.split(':')
  const hm = `${Number(m)} ${Number(h)}`
  if (c.preset === 'weekdays') return `${hm} * * 1-5`
  if (c.preset === 'daily') return `${hm} * * *`
  if (c.preset === 'weekly') return `${hm} * * ${c.day}`
  return c.cron.trim()
}

export function fromCron(cron: string): Cadence {
  const match = /^(\d{1,2}) (\d{1,2}) \* \* (\*|1-5|[0-6])$/.exec(cron.trim())
  if (!match) return { preset: 'custom', time: '09:00', day: '1', cron }
  const [, m = '0', h = '9', dow = '*'] = match
  const time = `${h.padStart(2, '0')}:${m.padStart(2, '0')}`
  if (dow === '*') return { preset: 'daily', time, day: '1', cron }
  if (dow === '1-5') return { preset: 'weekdays', time, day: '1', cron }
  return { preset: 'weekly', time, day: dow, cron }
}
