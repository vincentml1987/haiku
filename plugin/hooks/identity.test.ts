import { test, expect } from 'claude-code/testing'
import { normalizeHome, homeMismatch } from './identity'

test('unset expectedHome never blocks (the guard is opt-in)', async () => {
  expect(homeMismatch('', 'C:\\anywhere', 'Qualia')).toBe(null)
  expect(homeMismatch('   ', 'C:\\anywhere', 'Qualia')).toBe(null)
})

test('same folder passes regardless of case, slash style and trailing separator', async () => {
  const home = 'C:\\Users\\alice\\Home\\AI Homes\\Moxie'
  expect(homeMismatch(home, home, 'Moxie')).toBe(null)
  expect(homeMismatch(home, 'c:/users/alice/home/ai homes/moxie/', 'Moxie')).toBe(null)
  expect(homeMismatch(home + '\\', home.toUpperCase(), 'Moxie')).toBe(null)
})

test('the 2026-10-04 incident: Moxie folder with Tessera settings is refused', async () => {
  const err = homeMismatch(
    'C:\\Users\\alice\\Home\\AI Homes\\Tessera',
    'C:\\Users\\alice\\Home\\AI Homes\\Moxie',
    'Tessera',
  )
  expect(err).not.toBe(null)
  expect(err!).toContain('Moxie')
  expect(err!).toContain('Tessera')
})

test('a parent or child folder is not a match', async () => {
  const home = 'C:\\AIs\\Qualia'
  expect(homeMismatch(home, 'C:\\AIs', 'Qualia')).not.toBe(null)
  expect(homeMismatch(home, 'C:\\AIs\\Qualia\\Qualia', 'Qualia')).not.toBe(null)
  expect(homeMismatch(home, 'C:\\AIs\\Qualia2', 'Qualia')).not.toBe(null)
})

test('normalizeHome keeps a drive root and a UNC prefix intact', async () => {
  expect(normalizeHome('C:\\')).toBe('c:\\')
  expect(normalizeHome('\\\\server\\share\\x\\')).toBe('\\\\server\\share\\x')
  expect(normalizeHome('/home/someone/')).toBe('\\home\\someone')
})
