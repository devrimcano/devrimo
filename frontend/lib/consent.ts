/**
 * Whether this visitor has agreed to be measured, and to what.
 *
 * Measured on the deployed site before this existed: opening /login, signed
 * out, having agreed to nothing, left behind a PostHog cookie carrying a
 * persistent `$device_id` and `distinct_id`, a matching localStorage record,
 * three sessionStorage keys, and the server-side configuration for autocapture,
 * heatmaps, dead clicks, web vitals, exception capture and session recording.
 *
 * KVKK's cookie guidance (Çerez Uygulamaları Hakkında Rehber) sets three rules
 * this file exists to satisfy:
 *
 *   - Cookies without which the requested service cannot be delivered - the
 *     Supabase session, the theme, the locale, and the record of this very
 *     choice - rest on a legal ground other than consent. They are disclosed,
 *     not offered for refusal, and nothing here can switch them off.
 *   - Everything else needs consent asked for *before* anything is written,
 *     as easy to refuse as to give, and withdrawable just as easily.
 *   - The optional categories must be switchable one by one, not as a single
 *     all-or-nothing bargain. Hence two of them, because this app has two
 *     genuinely different asks: counting what gets used, and recording the
 *     screen while it happens.
 */

/** The optional categories. Necessary cookies are deliberately absent. */
export type ConsentCategories = {
  /** Pageviews, clicks, heatmaps, web vitals, and uncaught exceptions. */
  measurement: boolean;
  /** Session replay: a recording of the screen, with inputs masked. */
  replay: boolean;
};

export type Consent = ConsentCategories & {
  /** False before the question has been answered — which is not the same as "no". */
  decided: boolean;
};

export const CONSENT_COOKIE = "devrimo-cerez-tercihi";

/**
 * Twelve months, the longest the KVKK guidance treats as reasonable for a
 * consent record before the question should be put again.
 */
const CONSENT_MAX_AGE_SECONDS = 60 * 60 * 24 * 365;

/** Fired on this tab when the choice changes, so the SDK can start or stop. */
export const CONSENT_EVENT = "devrimo:consent";

/** Not readable yet, because there is no document to read: the server render. */
const UNKNOWN = "unknown";
/** Readable, and empty: the question is open. */
const UNSET = "unset";

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const prefix = `${name}=`;
  for (const part of document.cookie.split("; ")) {
    if (part.startsWith(prefix)) return decodeURIComponent(part.slice(prefix.length));
  }
  return null;
}

/**
 * The stored answer, as the raw string it is stored as.
 *
 * A string and not an object on purpose: `useSyncExternalStore` compares
 * snapshots by identity, and a function returning a fresh object every call
 * re-renders for ever. Components parse it, memoised, at the edge.
 */
export function consentSnapshot(): string {
  return readCookie(CONSENT_COOKIE) ?? UNSET;
}

export function consentServerSnapshot(): string {
  return UNKNOWN;
}

/** `null` while the answer is not yet readable, so nothing renders on a guess. */
export function parseConsent(raw: string): Consent | null {
  if (raw === UNKNOWN) return null;
  const parts = new Set(raw.split(",").filter(Boolean));
  const measurement = parts.has("measurement");
  return {
    decided: raw !== UNSET,
    measurement,
    // Replay is a recording of a session the measurement SDK has to be running
    // to make. Granting it alone is not a state this can be in.
    replay: measurement && parts.has("replay"),
  };
}

export function writeConsent(categories: ConsentCategories): void {
  if (typeof document === "undefined") return;
  const granted: string[] = [];
  if (categories.measurement) granted.push("measurement");
  if (categories.measurement && categories.replay) granted.push("replay");
  // "none" rather than an empty value, so a refusal is a recorded answer and
  // not an unanswered question that asks again on the next page.
  const value = granted.length ? granted.join(",") : "none";

  // Lax rather than Strict: a student following a password-reset link from
  // their mail client arrives cross-site, and having the banner reappear
  // because of that would be asking a question already answered.
  const secure = location.protocol === "https:" ? "; Secure" : "";
  document.cookie =
    `${CONSENT_COOKIE}=${value}; Path=/; Max-Age=${CONSENT_MAX_AGE_SECONDS}; SameSite=Lax${secure}`;
  window.dispatchEvent(new CustomEvent<string>(CONSENT_EVENT, { detail: value }));
}

/**
 * The cookie, as an external store React can subscribe to.
 *
 * Two components read it - the banner, and the preference control on the notice
 * and settings pages - and a choice made in one has to show in the other at
 * once. `storage` covers the same site open in a second tab.
 */
export function subscribeConsent(onChange: () => void): () => void {
  window.addEventListener(CONSENT_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(CONSENT_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

/**
 * Remove what a previous "yes" left behind.
 *
 * Withdrawing consent has to actually undo something, or it is a checkbox
 * rather than a right. Everything PostHog persists is prefixed with its project
 * key, so it is cleared by prefix rather than by naming the keys - because
 * those names change between SDK versions, and a name this file failed to keep
 * up with would be a tracker that survived its own deletion.
 */
export function clearMeasurementStorage(): void {
  if (typeof document === "undefined") return;
  const isMeasurement = (name: string) => name.startsWith("ph_") || name.startsWith("__ph");

  for (const part of document.cookie.split("; ")) {
    const name = part.split("=")[0];
    if (!isMeasurement(name)) continue;
    // Cleared on the exact host and on the parent domain, because the SDK may
    // have set either, and a cookie deleted at the wrong scope is a cookie that
    // is still there.
    for (const domain of [location.hostname, `.${location.hostname}`]) {
      document.cookie = `${name}=; Path=/; Max-Age=0; SameSite=Lax; Domain=${domain}`;
    }
    document.cookie = `${name}=; Path=/; Max-Age=0; SameSite=Lax`;
  }

  for (const store of [localStorage, sessionStorage]) {
    try {
      for (const name of Object.keys(store)) {
        if (isMeasurement(name)) store.removeItem(name);
      }
    } catch {
      // Storage can be unavailable (private mode, blocked site data). Nothing
      // was written in that case either.
    }
  }
}
