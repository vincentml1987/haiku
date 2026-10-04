/**
 * Context-usage reminders (eot-initialization-automation, 2026-10-04). No `$`
 * here, same rule as wake.ts: register.ts reads `$.session.usage()` and hands
 * this module plain numbers, so the rules are unit-testable without an engine.
 *
 * A reminder only asks the member to decide whether an EOT makes sense. It
 * never acts for them. The thresholds are placeholders (Teddy: retune once we
 * know what the percent means in real Claude Code behavior).
 */

export const DEFAULT_THRESHOLDS = [60, 75, 85, 92]

/** Persisted per participant in $.store. Keyed by the session's start time,
 * not by member: `/clear` starts a session over (SessionUsage.startedAt), so
 * a cleared session gets fresh reminders instead of staying silenced. */
export type ReminderState = {
  /** SessionUsage.startedAt of the session these figures belong to */
  sessionKey: number
  /** thresholds already reminded for in this session */
  fired: number[]
}

export const EMPTY_REMINDER_STATE: ReminderState = { sessionKey: 0, fired: [] }

/** Parse the `reminderThresholds` option: "60,75,85,92". Anything unusable
 * falls back to the defaults; "off" or "none" disables reminders. */
export function parseThresholds(v: unknown): number[] {
  if (v == null || v === '') return DEFAULT_THRESHOLDS
  const s = String(v).trim().toLowerCase()
  if (s === 'off' || s === 'none') return []
  const nums = s.split(/[\s,]+/).map(Number).filter(n => Number.isFinite(n) && n > 0 && n <= 100)
  return nums.length ? [...new Set(nums)].sort((a, b) => a - b) : DEFAULT_THRESHOLDS
}

export type ReminderDecision = {
  /** the line to inject, or null for none */
  text: string | null
  /** state to persist */
  state: ReminderState
}

/** `percent` is undefined before a window's first response: skip, never 0. */
export function decideReminder(
  state: ReminderState,
  sessionKey: number,
  percent: number | undefined,
  thresholds: number[],
): ReminderDecision {
  const cur = state.sessionKey === sessionKey ? state : { sessionKey, fired: [] }
  if (percent == null || !Number.isFinite(percent)) return { text: null, state: cur }
  const due = thresholds.filter(t => percent >= t && !cur.fired.includes(t))
  if (due.length === 0) return { text: null, state: cur }
  // Several crossed at once (a big jump): fire for the highest, mark all.
  const top = due[due.length - 1]
  const step = thresholds.indexOf(top) + 1
  const first = cur.fired.length === 0 && due.length === thresholds.filter(t => percent >= t).length
  const body = first
    ? `Context is at ${percent}%. Check whether an EOT makes sense now. It is your call, and "not now" is a fine answer during deadline work. A reminder never acts for you. You can run the whole cycle yourself whenever you choose.`
    : `Context at ${percent}% (reminder ${step} of ${thresholds.length}). Is an EOT due? Your call.`
  return {
    text: `HAIKU context reminder (from the plugin, not your user): ${body}`,
    state: { sessionKey, fired: [...cur.fired, ...due] },
  }
}
