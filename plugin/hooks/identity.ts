/**
 * Home-folder identity guard (2026-10-04). No `$` here, same rule as
 * client.ts and wake.ts: register.ts reads the session's cwd and hands it in.
 *
 * Why: expectedName alone cannot catch a wrong --settings file, because the
 * wrong file carries its own matching name (Moxie launched with
 * tessera.settings.json and posted as Tessera; expectedName and
 * participantName were both "Tessera"). The session's working folder is
 * the one thing the settings file does not control, so it is what we check.
 */

/** Normalizes a path for comparison: one separator style, no trailing
 * separator, and case-folded when it looks like a Windows path (drive
 * letter or UNC), since Windows paths are case-insensitive. */
export function normalizeHome(p: string): string {
  let s = p.trim().replace(/\//g, '\\')
  // collapse repeated separators, but keep a leading UNC "\\"
  const unc = s.startsWith('\\\\')
  s = s.replace(/\\{2,}/g, '\\')
  if (unc) s = '\\' + s
  while (s.length > 3 && s.endsWith('\\')) s = s.slice(0, -1)
  const windowsish = /^[a-zA-Z]:/.test(s) || unc
  return windowsish ? s.toLowerCase() : s
}

/** null when the guard passes (or is not configured); otherwise the error
 * text every HAIKU call should fail with. */
export function homeMismatch(expectedHome: string, cwd: string, participantName: string): string | null {
  if (!expectedHome.trim()) return null
  if (normalizeHome(expectedHome) === normalizeHome(cwd)) return null
  return (
    `HAIKU identity guard: this session runs in "${cwd}", but the settings file for ` +
    `"${participantName}" expects "${expectedHome}". Wrong --settings file, or the home ` +
    `moved without updating expectedHome? Refusing to act as "${participantName}".`
  )
}
