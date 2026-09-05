import { NextResponse } from "next/server";
import { listChatSessions } from "@/lib/api/chat";
import { apiErrorResponse, authenticatedRoute } from "@/lib/api/route-utils";

export const GET = authenticatedRoute("/api/sessions", async (context) => {
  try {
    const sessions = await listChatSessions(context.auth.accessToken);
    return NextResponse.json({ sessions });
  } catch (error) {
    return apiErrorResponse(error, context);
  }
});
