import { proxyRoute } from "@/lib/api/authenticated-proxy";

const proxy = proxyRoute("schedule");

export const GET = proxy;
export const POST = proxy;
