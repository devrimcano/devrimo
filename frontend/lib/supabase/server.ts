import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";
import { getSupabaseAnonKey, getSupabaseUrl, supabaseClientOptions } from "@/lib/env";

export async function createClient() {
  const cookieStore = await cookies();

  return createServerClient(getSupabaseUrl(), getSupabaseAnonKey(), {
    ...supabaseClientOptions(),
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      setAll(cookiesToSet) {
        try {
          cookiesToSet.forEach(({ name, value, options }) =>
            cookieStore.set(name, value, options),
          );
        } catch {
          // Called from a Server Component; middleware refreshes the session.
        }
      },
    },
  });
}

export async function getAuth() {
  const supabase = await createClient();
  const { data: claimsData, error: claimsError } = await supabase.auth.getClaims();
  const claims = claimsData?.claims;

  if (claimsError || typeof claims?.sub !== "string") return null;

  const {
    data: { session },
    error: sessionError,
  } = await supabase.auth.getSession();

  // getClaims() verifies identity. getSession() is used only for the raw
  // access token that the Next.js proxy forwards to the broker; its cookie-
  // sourced user object must not be trusted or accessed on the server.
  if (sessionError || !session?.access_token) return null;

  return {
    supabase,
    user: {
      id: claims.sub,
      email: typeof claims.email === "string" ? claims.email : undefined,
    },
    accessToken: session.access_token,
  };
}
