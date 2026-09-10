import type { MetadataRoute } from "next";
import { getSiteUrl } from "@/lib/env";
import { LEGAL_DOCUMENTS } from "@/lib/legal/documents";

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
        // What exists for someone who is not signed in: the door, the notice
        // explaining what the door sets on their machine, and whichever legal
        // texts are finished. A privacy notice nobody can find is not a notice.
        //
        // Derived from the registry rather than listed here, so a text becomes
        // findable by being published and not by someone remembering to edit
        // this file — and, more importantly, so a draft cannot become findable
        // by the same oversight.
        allow: [
          "/login",
          "/gizlilik",
          ...LEGAL_DOCUMENTS.filter((document) => document.status === "published").map(
            (document) => `/belgeler/${document.slug}`,
          ),
        ],
        disallow: [
          "/", // the assistant, and the only page a signed-out visitor is sent from
          "/schedule",
          "/settings",
          "/updates",
          "/admin",
          "/api/",
          "/auth/",
          // Every draft, by name. The pages also carry `noindex`; this is the
          // half a crawler reads before requesting them at all.
          ...LEGAL_DOCUMENTS.filter((document) => document.status === "draft").map(
            (document) => `/belgeler/${document.slug}`,
          ),
        ],
      },
    ],
    // Absolute, as the format requires. Omitted entirely when the deployment
    // has not been told its own address, rather than guessed from a request.
    host: site,
  };
}
