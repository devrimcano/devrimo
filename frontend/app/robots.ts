import type { MetadataRoute } from "next";
import { getSiteUrl } from "@/lib/env";

/**
 * What a crawler is invited to read, which here is one page.
 *
 * /robots.txt answered 404 until now, so every crawler applied its own
 * default - fetch everything - to an application where everything except the
 * sign-in screen is a student's own data behind a session. Nothing was leaked
 * by that (a crawler has no session, and every route redirects or answers 401),
 * but it means the schedule, the chat, the settings and the admin panel were
 * all being requested, repeatedly, by anything that found the domain.
 *
 * So: the sign-in page is the site as far as a crawler is concerned, and the
 * rest is asked for by name rather than left to a wildcard, because a rule a
 * person can read is a rule someone will maintain.
 */
export default function robots(): MetadataRoute.Robots {
  const site = getSiteUrl() || undefined;
  return {
    rules: [
      {
        userAgent: "*",
        allow: "/login",
        disallow: [
          "/", // the assistant, and the only page a signed-out visitor is sent from
          "/schedule",
          "/settings",
          "/updates",
          "/admin",
          "/api/",
          "/auth/",
        ],
      },
    ],
    // Absolute, as the format requires. Omitted entirely when the deployment
    // has not been told its own address, rather than guessed from a request.
    host: site,
  };
}
