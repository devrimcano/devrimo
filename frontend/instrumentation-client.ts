/**
 * Client-side instrumentation, which now asks before it measures.
 *
 * Next.js runs this once, before the app renders, and that timing is why the
 * PostHog configuration used to live here: autocapture, session replay and
 * exception capture all need the SDK live before the first interaction.
 *
 * The timing is also exactly the problem. Running here meant the SDK wrote a
 * persistent device identifier for every visitor who had agreed to nothing, on
 * a page they had not signed in to. Under KVKK's cookie guidance, analytics and
 * session recording are not cookies the service cannot be delivered without;
 * they need consent asked for first. So startup only *resumes* what a stored
 * answer already permits - see lib/consent.ts - and the banner starts it for
 * anyone who agrees later, which is early enough to be useful and late enough
 * to have been asked.
 */

import { applyConsent } from "@/lib/analytics";
import { consentSnapshot, parseConsent } from "@/lib/consent";

const consent = parseConsent(consentSnapshot());
if (consent?.decided) {
  applyConsent(consent);
}
