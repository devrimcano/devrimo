import { NextResponse } from "next/server";
import { clearMemories, listMemories } from "@/lib/api/memories";
import { apiErrorResponse, authenticatedRoute } from "@/lib/api/route-utils";

const ROUTE = "/api/memories";

export const GET = authenticatedRoute(ROUTE, async (context) => {
  try {
    return NextResponse.json(await listMemories(context.auth.accessToken));
  } catch (error) {
    return apiErrorResponse(error, context);
  }
});

export const DELETE = authenticatedRoute(ROUTE, async (context) => {
  try {
    return NextResponse.json(await clearMemories(context.auth.accessToken));
  } catch (error) {
    return apiErrorResponse(error, context);
  }
});
