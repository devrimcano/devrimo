export function getSupabaseUrl() {
  return process.env.NEXT_PUBLIC_SUPABASE_URL ?? "";
}

export function getSupabaseAnonKey() {
  return (
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ??
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY ??
    ""
  );
}

/**
 * The public URL users should return to after an auth flow.
 *
 * This must be explicit in production because the request URL can contain an
 * internal bind address when the app is behind a reverse proxy.
 */
export function getSiteUrl() {
  const configured = process.env.NEXT_PUBLIC_SITE_URL?.trim();
  if (!configured) return "";

  try {
    const url = new URL(configured);
    if (url.protocol !== "http:" && url.protocol !== "https:") return "";
    return url.origin;
  } catch {
    return "";
  }
}

/**
 * The name Supabase stores this deployment's auth cookie under.
 *
 * Cookies are scoped by host, and a port is not part of a host. So two
 * development stacks on `localhost:3000` and `localhost:3001` share one cookie
 * jar: signing into the second silently overwrites the first's session, and the
 * first tab then behaves as the second's user. With one worktree per task and
 * two synthetic students per worktree, that is not an edge case — it is what
 * happens the first time somebody opens two stacks.
 *
 * `node scripts/dev.mjs setup` sets this per worktree. Empty in production,
 * where there is one origin and Supabase's default name is correct.
 */
export function getSupabaseCookieName() {
  return process.env.NEXT_PUBLIC_SUPABASE_COOKIE_NAME?.trim() ?? "";
}

/**
 * Options every Supabase client in this app must be constructed with.
 *
 * Shared rather than repeated because the browser client, the server client and
 * the middleware client must agree on the cookie name. Two of three agreeing
 * produces a session that exists on the server and not in the browser, which
 * presents as an intermittent redirect loop.
 */
export function supabaseClientOptions() {
  const name = getSupabaseCookieName();
  return name ? { cookieOptions: { name } } : {};
}

export function isSupabaseConfigured() {
  const url = getSupabaseUrl();
  const key = getSupabaseAnonKey();
  return Boolean(url && key && !url.includes("your-project"));
}

export function getApiBaseUrl() {
  return process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || "http://localhost:8000";
}

export function getPostHogKey() {
  return process.env.NEXT_PUBLIC_POSTHOG_KEY ?? "";
}

export function getPostHogHost() {
  // EU cloud. Kept as a variable rather than a constant because a self-hosted
  // instance or a reverse proxy changes it, and hardcoding it there means
  // events go to the wrong place silently.
  return process.env.NEXT_PUBLIC_POSTHOG_HOST || "https://eu.i.posthog.com";
}

export function isPostHogConfigured() {
  return Boolean(getPostHogKey());
}

/**
 * Hostnames PostHog attaches tracing headers to.
 *
 * The browser only ever calls this app's own origin — `app/api/**` proxies
 * everything through to the FastAPI broker — so this is the app's hostname,
 * never the broker's. Ports are not part of a hostname: "localhost:3000" would
 * match nothing at all.
 *
 * The current hostname is always included. The previous default covered only
 * `localhost` and `127.0.0.1`, so unless `NEXT_PUBLIC_POSTHOG_TRACING_HOSTS`
 * happened to be set correctly, the deployed app attached no tracing headers
 * at all and every backend trace was orphaned from the session that caused it.
 * Including it is safe precisely because the browser only ever calls its own
 * origin: the headers cannot reach a third party this way.
 */
export function getTracingHostnames() {
  const configured = process.env.NEXT_PUBLIC_POSTHOG_TRACING_HOSTS;
  const hosts = new Set(
    configured
      ? configured.split(",").map((host) => host.trim()).filter(Boolean)
      : ["localhost", "127.0.0.1"],
  );

  const current = typeof window !== "undefined" ? window.location.hostname : "";
  if (current) hosts.add(current);

  const site = getSiteUrl();
  if (site) {
    try {
      hosts.add(new URL(site).hostname);
    } catch {
      // getSiteUrl already validated this; ignore anything that slipped past.
    }
  }

  return [...hosts];
}
