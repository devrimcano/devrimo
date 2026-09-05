import { NextResponse } from "next/server";
import { deleteChatSession, getChatSession } from "@/lib/api/chat";
import { apiErrorResponse, authenticatedRoute } from "@/lib/api/route-utils";

type RouteContext = { params: Promise<{ id: string }> };

const ROUTE = "/api/sessions/[id]";

export const GET = authenticatedRoute(
  ROUTE,
  async (telemetry, _request, context: RouteContext) => {
    try {
      const { id } = await context.params;
      const session = await getChatSession(telemetry.auth.accessToken, id);
      return NextResponse.json(session);
    } catch (error) {
      return apiErrorResponse(error, telemetry);
    }
  },
);

export const DELETE = authenticatedRoute(
  ROUTE,
  async (telemetry, _request, context: RouteContext) => {
    try {
      const { id } = await context.params;
      await deleteChatSession(telemetry.auth.accessToken, id);
      return NextResponse.json({ ok: true });
    } catch (error) {
      return apiErrorResponse(error, telemetry);
    }
  },
);
