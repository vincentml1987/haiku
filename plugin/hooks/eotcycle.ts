/**
 * EOT > Clear > Restore cycle: the gate (eot-initialization-automation,
 * 2026-10-04). No `$` here, same rule as wake.ts and reminder.ts: register.ts
 * gathers the facts (a directory listing, two git calls) and hands this
 * module plain data, so the rules are unit-testable without an engine.
 *
 * The member writes their own EOT; this only checks it and, later, clears.
 * Fail closed: any missing config or failed check means no clear.
 */

export const RESTORE_PROMPT = 'initialize from your latest EOT'
export const DEFAULT_MAX_AGE_MINUTES = 10

/** Per-member gate, read from the identity's --settings file. */
export type EotConfig = {
  /** absolute folder holding the member's EOT journals */
  dir: string
  /** "git": also needs a G signature and a pushed commit. "file": existence/size/age only. */
  kind: 'git' | 'file'
  /** git kind: the repo root the journals live in */
  repo: string
  /** git kind: the allowed_signers file used to read the signature as G */
  allowedSigners: string
  /** an EOT older than this is not "the EOT for this moment" */
  maxAgeMinutes: number
  /** filename pattern for a journal, "EOT Journal - YYYY-MM-DD HHmm.md" */
  namePattern: RegExp
}

export const EOT_NAME = /^EOT Journal - \d{4}-\d{2}-\d{2} \d{4}\.md$/

/** Returns the config or the reason it is unusable. Never guesses a path. */
export function parseEotConfig(o: Record<string, unknown>): { ok: true; config: EotConfig } | { ok: false; reason: string } {
  const dir = String(o.eotDir ?? '').trim()
  if (!dir) return { ok: false, reason: "eotDir is not set in this identity's settings file, so there is no EOT folder to check" }
  const kind = String(o.eotKind ?? 'git').trim().toLowerCase()
  if (kind !== 'git' && kind !== 'file') return { ok: false, reason: `eotKind must be "git" or "file", not "${kind}"` }
  const repo = String(o.eotRepo ?? '').trim()
  const allowedSigners = String(o.eotAllowedSigners ?? '').trim()
  if (kind === 'git' && (!repo || !allowedSigners)) {
    return { ok: false, reason: 'eotKind "git" needs eotRepo and eotAllowedSigners in the settings file' }
  }
  const n = Number(o.eotMaxAgeMinutes ?? DEFAULT_MAX_AGE_MINUTES)
  const maxAgeMinutes = Number.isFinite(n) && n > 0 ? n : DEFAULT_MAX_AGE_MINUTES
  return { ok: true, config: { dir, kind, repo, allowedSigners, maxAgeMinutes, namePattern: EOT_NAME } }
}

export type DirEntry = { name: string; kind?: string; size: number; mtimeMs: number }

/** The newest journal by modification time among names matching the pattern. */
export function pickLatest(entries: DirEntry[], pattern: RegExp): DirEntry | null {
  const hits = entries.filter(e => e.kind !== 'dir' && pattern.test(e.name))
  if (hits.length === 0) return null
  return hits.reduce((a, b) => (b.mtimeMs > a.mtimeMs ? b : a))
}

/** What register.ts could learn about the git side; absent for kind "file". */
export type GitFacts = {
  /** %G? of the commit that last touched the file; "" when the file is untracked */
  signature: string
  /** the file has uncommitted changes or is untracked */
  isDirty: boolean
  /** first line of `git status -sb` mentions "ahead" */
  isAhead: boolean
}

export type GateCheck = { name: string; ok: boolean; detail: string }
export type GateResult = { ok: boolean; file: string | null; checks: GateCheck[] }

export function evaluateGate(cfg: EotConfig, latest: DirEntry | null, git: GitFacts | null, nowMs: number): GateResult {
  const checks: GateCheck[] = []
  if (!latest) {
    checks.push({ name: 'exists', ok: false, detail: `no file matching "EOT Journal - YYYY-MM-DD HHmm.md" in ${cfg.dir}` })
    return { ok: false, file: null, checks }
  }
  checks.push({ name: 'exists', ok: true, detail: latest.name })
  checks.push({ name: 'non-empty', ok: latest.size > 0, detail: `${latest.size} bytes` })
  const ageMin = (nowMs - latest.mtimeMs) / 60000
  checks.push({
    name: 'fresh',
    ok: ageMin >= 0 && ageMin <= cfg.maxAgeMinutes,
    detail: `modified ${ageMin.toFixed(1)} min ago, limit ${cfg.maxAgeMinutes}`,
  })
  if (cfg.kind === 'git') {
    if (!git) {
      checks.push({ name: 'git', ok: false, detail: 'git facts unavailable' })
    } else {
      checks.push({ name: 'committed', ok: !git.isDirty && git.signature !== '', detail: git.isDirty ? 'uncommitted or untracked' : 'clean' })
      checks.push({ name: 'signed G', ok: git.signature === 'G', detail: `signature status "${git.signature || 'none'}"` })
      checks.push({ name: 'pushed', ok: !git.isAhead, detail: git.isAhead ? 'branch is ahead of origin' : 'level with origin' })
    }
  }
  return { ok: checks.every(c => c.ok), file: latest.name, checks }
}

export function formatGate(g: GateResult): string {
  return g.checks.map(c => `${c.ok ? 'pass' : 'FAIL'}  ${c.name}: ${c.detail}`).join('\n')
}

/** What the live cycle would do after a passing gate, in order. */
export function planText(): string {
  return `then /clear, then submit "${RESTORE_PROMPT}"`
}
