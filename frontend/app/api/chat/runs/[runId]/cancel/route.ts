import { NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { authenticatedRoute } from "@/lib/api/route-utils";

export const POST = authenticatedRoute("/api/chat/runs/[runId]/cancel", async (context, request) => {
  const runId = new URL(request.url).pathname.split("/").at(-2);
  if (!runId || !/^[0-9a-f-]{36}$/i.test(runId)) return NextResponse.json({ detail: "Invalid run" }, { status: 422 });
  return NextResponse.json(await apiFetch(`/chat/runs/${runId}/cancel`, { method: "POST", token: context.auth.accessToken }));
});
