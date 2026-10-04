import { test, expect } from 'claude-code/testing'
import { decideReminder, parseThresholds, EMPTY_REMINDER_STATE, DEFAULT_THRESHOLDS } from './reminder'

test('no reminder below the first threshold or with no reading', () => {
  expect(decideReminder(EMPTY_REMINDER_STATE, 1, 59, DEFAULT_THRESHOLDS).text).toBe(null)
  expect(decideReminder(EMPTY_REMINDER_STATE, 1, undefined, DEFAULT_THRESHOLDS).text).toBe(null)
})

test('each threshold fires once per session', () => {
  const a = decideReminder(EMPTY_REMINDER_STATE, 1, 61, DEFAULT_THRESHOLDS)
  expect(a.text).toContain('61%')
  expect(decideReminder(a.state, 1, 70, DEFAULT_THRESHOLDS).text).toBe(null)
  const b = decideReminder(a.state, 1, 76, DEFAULT_THRESHOLDS)
  expect(b.text).toContain('reminder 2 of 4')
})

test('a jump past several thresholds fires once for the highest', () => {
  const a = decideReminder(EMPTY_REMINDER_STATE, 1, 90, DEFAULT_THRESHOLDS)
  expect(a.text).toContain('90%')
  expect(a.state.fired).toEqual([60, 75, 85])
})

test('a new session key (after /clear) gets fresh reminders', () => {
  const a = decideReminder(EMPTY_REMINDER_STATE, 1, 95, DEFAULT_THRESHOLDS)
  expect(decideReminder(a.state, 1, 95, DEFAULT_THRESHOLDS).text).toBe(null)
  expect(decideReminder(a.state, 2, 61, DEFAULT_THRESHOLDS).text).not.toBe(null)
})

test('parseThresholds: defaults, custom, off, junk', () => {
  expect(parseThresholds(undefined)).toEqual(DEFAULT_THRESHOLDS)
  expect(parseThresholds('50, 80')).toEqual([50, 80])
  expect(parseThresholds('off')).toEqual([])
  expect(parseThresholds('abc')).toEqual(DEFAULT_THRESHOLDS)
})
