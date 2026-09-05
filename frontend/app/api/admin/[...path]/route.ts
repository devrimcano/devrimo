import { proxyRoute } from "@/lib/api/authenticated-proxy";

const proxy = proxyRoute("admin");

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
