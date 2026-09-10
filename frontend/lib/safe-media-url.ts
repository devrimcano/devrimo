/**
 * Whether a URL that came from a model or a tool is safe to put on an anchor.
 *
 * Every image and file in a message is content this application did not write.
 * The assistant reads METU course pages, ODTÜClass and the student's webmail —
 * untrusted text, some of it written by strangers — and anything that reaches
 * the model's output can reach a message part. So the URL in a part is attacker
 * input in the ordinary case, not the exotic one.
 *
 * `javascript:` is the scheme that matters. Setting it on an anchor and calling
 * `click()` runs the script in this origin, and this origin's session cookie is
 * script-readable by necessity — @supabase/ssr requires it. A download button is
 * enough of a click.
 *
 * Allowed, deliberately narrow:
 *   - `https:` and `http:`, which are what a real remote image is;
 *   - `blob:`, which this app makes itself from a data URI it already parsed;
 *   - `data:`, only for images, because that is how the model returns one
 *     inline and a `data:image/...` cannot execute.
 *
 * Everything else — `javascript:`, `vbscript:`, `file:`, a bare `data:text/html`
 * — is refused, and the caller shows nothing rather than something dangerous.
 */
export function isSafeMediaUrl(raw: string): boolean {
  if (typeof raw !== "string" || !raw.trim()) return false;

  // A data URI is checked by shape rather than by parsing: `new URL` accepts
  // any of them, and what matters is the media type it declares.
  if (/^data:/i.test(raw)) return /^data:image\/[a-z0-9.+-]+\s*;/i.test(raw) || /^data:image\/[a-z0-9.+-]+\s*,/i.test(raw);

  try {
    // Resolved against the current origin so a relative path is judged as the
    // absolute URL it becomes, and a scheme smuggled in with whitespace or
    // control characters — "java\tscript:" — is normalised before it is read.
    const url = new URL(raw, typeof location === "undefined" ? "https://devrimo.invalid" : location.href);
    return url.protocol === "https:" || url.protocol === "http:" || url.protocol === "blob:";
  } catch {
    return false;
  }
}
