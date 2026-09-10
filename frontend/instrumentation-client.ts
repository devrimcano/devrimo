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
import { clearMeasurementStorage, consentSnapshot, parseConsent } from "@/lib/consent";

const consent = parseConsent(consentSnapshot());
if (consent?.decided) {
  applyConsent(consent);
} else if (consent) {
  // Everyone who visited before the question existed is still carrying the
  // identifier that was written without asking them. Measured on the live site
  // after this shipped: the banner shows, the SDK never starts, nothing is sent
  // — and the old `ph_…` cookie is still there, holding a $device_id and a
  // distinct_id from before consent was a thing.
  //
  // Nothing is leaking while it sits there. It matters because of what happens
  // next: without this, a later "yes" would quietly re-attach the person to an
  // identity collected without one, which is not the fresh start that answering
  // the question should be. So the slate is cleared while the question is open.
  clearMeasurementStorage();
}
