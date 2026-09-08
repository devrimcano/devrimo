import { proxyRoute } from "@/lib/api/authenticated-proxy";

// Reading a section's eligibility table is one SAIS page per section, and a
// course can have forty-five of them. Cached for a week and shared across
// students, so this is the first reader's cost rather than everyone's — but
// the first reader must not be cut off mid-flight and get nothing.
export const maxDuration = 300;

const proxy = proxyRoute("schedule");

export const GET = proxy;
export const POST = proxy;
// The planner saves its timetable here so chat can talk about it.
export const PUT = proxy;
export const PATCH = proxy;
