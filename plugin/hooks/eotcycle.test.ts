import { test, expect } from 'claude-code/testing'
import { parseEotConfig, pickLatest, evaluateGate, EOT_NAME } from './eotcycle'

const NOW = 10_000_000
const base = { eotDir: 'C:/x/EOT Journals', eotKind: 'git', eotRepo: 'C:/x', eotAllowedSigners: 'C:/x/allowed_signers' }
const cfg = (o: Record<string, unknown> = {}) => {
  const r = parseEotConfig({ ...base, ...o })
  if (!r.ok) throw new Error(r.reason)
  return r.config
}
const f = (ageMin: number, size = 100) => ({ name: 'EOT Journal - 2026-10-04 1829.md', size, mtimeMs: NOW - ageMin * 60000 })
const goodGit = { signature: 'G', isDirty: false, isAhead: false }

test('config fails closed without a folder, or git without repo and signers', () => {
  expect(parseEotConfig({}).ok).toBe(false)
  expect(parseEotConfig({ eotDir: 'd', eotKind: 'git' }).ok).toBe(false)
  expect(parseEotConfig({ eotDir: 'd', eotKind: 'nope' }).ok).toBe(false)
  expect(parseEotConfig({ eotDir: 'd', eotKind: 'file' }).ok).toBe(true)
})

test('pickLatest takes the newest matching journal and ignores other files', () => {
  const a = { name: 'EOT Journal - 2026-10-04 1000.md', size: 1, mtimeMs: 5 }
  const b = { name: 'EOT Journal - 2026-10-04 1200.md', size: 1, mtimeMs: 9 }
  const c = { name: 'notes.md', size: 1, mtimeMs: 99 }
  expect(pickLatest([a, c, b], EOT_NAME)?.name).toBe(b.name)
  expect(pickLatest([c], EOT_NAME)).toBe(null)
})

test('gate passes for a fresh, non-empty, signed, pushed journal', () => {
  expect(evaluateGate(cfg(), f(2), goodGit, NOW).ok).toBe(true)
})

test('an old journal fails the freshness check (the 18:30-EOT case)', () => {
  const g = evaluateGate(cfg(), f(300), goodGit, NOW)
  expect(g.ok).toBe(false)
  expect(g.checks.find(c => c.name === 'fresh')?.ok).toBe(false)
})

test('each other failure blocks: empty, unsigned, dirty, ahead, missing, no git facts', () => {
  expect(evaluateGate(cfg(), f(1, 0), goodGit, NOW).ok).toBe(false)
  expect(evaluateGate(cfg(), f(1), { ...goodGit, signature: 'N' }, NOW).ok).toBe(false)
  expect(evaluateGate(cfg(), f(1), { ...goodGit, isDirty: true }, NOW).ok).toBe(false)
  expect(evaluateGate(cfg(), f(1), { ...goodGit, isAhead: true }, NOW).ok).toBe(false)
  expect(evaluateGate(cfg(), null, goodGit, NOW).ok).toBe(false)
  expect(evaluateGate(cfg(), f(1), null, NOW).ok).toBe(false)
})

test('file kind needs no git facts, and a future mtime is not fresh', () => {
  expect(evaluateGate(cfg({ eotKind: 'file' }), f(1), null, NOW).ok).toBe(true)
  expect(evaluateGate(cfg({ eotKind: 'file' }), f(-5), null, NOW).ok).toBe(false)
})
