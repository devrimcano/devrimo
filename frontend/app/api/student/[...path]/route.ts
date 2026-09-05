import { proxyRoute } from "@/lib/api/authenticated-proxy";

// A SAIS refresh spawns the student's campus servers and makes several
// round trips, so ten to thirty seconds is normal rather than a hang. The
// default cut the request off mid-flight, and the broker reported the
// cancellation as a 500 for work that had in fact completed.
export const maxDuration = 300;

const proxy = proxyRoute("student");

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
