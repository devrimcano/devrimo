import { createClient } from "@/lib/supabase/server";
import { getSiteUrl } from "@/lib/env";
import { localPath } from "@/lib/safe-next";
import { NextResponse } from "next/server";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const code = searchParams.get("code");
  const requestedNext = searchParams.get("next") ?? "/";
  const forwardedHost = request.headers.get("x-forwarded-host");
  const forwardedProto = request.headers.get("x-forwarded-proto") || "https";
  const origin =
    getSiteUrl() ||
    (forwardedHost ? `${forwardedProto}://${forwardedHost}` : new URL(request.url).origin);

  // Resolved against this origin rather than pattern-matched. The old test -
  // starts with "/" and not "//" - lets "/\elsewhere" through, and a browser
  // reads that as another origin. This route happened to survive it because it
  // prefixes the origin itself, but the rule it shared with the sign-in form
  // did not, so both now decide the same way.
  const next = localPath(requestedNext, origin);

  if (code) {
    const supabase = await createClient();
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (!error) {
      return NextResponse.redirect(`${origin}${next}`);
    }
  }

  return NextResponse.redirect(`${origin}/login?error=auth`);
}

