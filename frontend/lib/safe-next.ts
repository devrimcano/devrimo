/**
 * Where a "?next=" value is allowed to send someone.
 *
 * The test this replaces read `startsWith("/") && !startsWith("//")`, which
 * looks like it keeps the destination on this site and does not. A browser
 * resolves "/\\elsewhere" - and "/<tab>/elsewhere" - to http://elsewhere/, and
 * both spellings pass that test. Measured in a real browser on the live
 * origin, not argued from the specification.
 *
 * What that costs: a link like /login?next=/\lookalike takes a student through
 * the genuine sign-in form and hands them to someone else's page the instant
 * it succeeds. That is the most convincing phishing page there is, because the
 * person has just authenticated on the real site and has no reason to doubt
 * what follows.
 *
 * Resolving the value the way the browser will, and then requiring the result
 * to be this origin, closes the family rather than the two spellings anyone
 * happens to have thought of.
 */
export function localPath(raw: string | null | undefined, origin: string): string {
  const home = "/";
  if (!raw) return home;
  try {
    const base = new URL(origin);
    const resolved = new URL(raw, base);
    if (resolved.origin !== base.origin) return home;
    return `${resolved.pathname}${resolved.search}${resolved.hash}` || home;
  } catch {
    return home;
  }
}

/** The origin to resolve against, on a page or during server rendering. */
export function currentOrigin(): string {
  // No location on the server; an absolute URL then fails the origin
  // comparison, which is the safe direction to fail in.
  return typeof window === "undefined" ? "http://devrimo.invalid" : window.location.origin;
}
