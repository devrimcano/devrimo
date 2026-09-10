import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";
import {
  getSupabaseAnonKey,
  getSupabaseUrl,
  isSupabaseConfigured,
  supabaseClientOptions,
} from "@/lib/env";

export async function updateSession(request: NextRequest) {
  let response = NextResponse.next({ request });

  if (!isSupabaseConfigured()) {
    return response;
  }

  const supabase = createServerClient(getSupabaseUrl(), getSupabaseAnonKey(), {
    ...supabaseClientOptions(),
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      setAll(cookiesToSet) {
        cookiesToSet.forEach(({ name, value }) => request.cookies.set(name, value));
        response = NextResponse.next({ request });
        cookiesToSet.forEach(({ name, value, options }) =>
          response.cookies.set(name, value, options),
        );
      },
    },
  });

  const { data: claimsData } = await supabase.auth.getClaims();
  const hasVerifiedUser = typeof claimsData?.claims?.sub === "string";

  const isAuthRoute = request.nextUrl.pathname.startsWith("/login");
  const isPublic =
    isAuthRoute ||
    request.nextUrl.pathname.startsWith("/auth") ||
    request.nextUrl.pathname.startsWith("/api");

  // The pages this application actually serves. Everything else is a typo or a
  // scan, and sending those to the sign-in screen made the 404 unreachable
  // while signed out: /nope answered "sign in first", and signing in then
  // landed on the 404 anyway. A visitor should be asked to sign in for a page
  // that exists, and told it does not exist when it does not.
  const served = new Set(["", "schedule", "settings", "updates", "admin"]);
  const firstSegment = request.nextUrl.pathname.split("/")[1] ?? "";
  const isServedPage = served.has(firstSegment);

  if (!hasVerifiedUser && !isPublic && isServedPage) {
    const url = request.nextUrl.clone();
    const requestedNext = `${request.nextUrl.pathname}${request.nextUrl.search}`;
    url.pathname = "/login";
    url.search = "";
    url.searchParams.set("next", requestedNext);
    return NextResponse.redirect(url);
  }

  if (hasVerifiedUser && isAuthRoute) {
    const url = request.nextUrl.clone();
    url.pathname = "/";
    url.search = "";
    return NextResponse.redirect(url);
  }

  return response;
}
