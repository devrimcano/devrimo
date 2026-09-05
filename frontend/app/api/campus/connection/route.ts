import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import {
  deleteCampusConnection,
  getCampusConnection,
  putCampusConnection,
} from "@/lib/api/campus";
import { apiErrorResponse, authenticatedRoute } from "@/lib/api/route-utils";
import type { CampusConnectionInput } from "@/lib/types";

const ROUTE = "/api/campus/connection";

export const GET = authenticatedRoute(ROUTE, async (context) => {
  try {
    return NextResponse.json(await getCampusConnection(context.auth.accessToken));
  } catch (error) {
    return apiErrorResponse(error, context);
  }
});

/**
 * The student's METU password passes through this handler on its way to the
 * broker, and is never logged, cached, or echoed back — the broker's response
 * describes the connection without containing it.
 */
export const PUT = authenticatedRoute(ROUTE, async (context, request: NextRequest) => {
  try {
    const body = (await request.json()) as CampusConnectionInput;
    return NextResponse.json(await putCampusConnection(context.auth.accessToken, body));
  } catch (error) {
    return apiErrorResponse(error, context);
  }
});

export const DELETE = authenticatedRoute(ROUTE, async (context) => {
  try {
    return NextResponse.json(await deleteCampusConnection(context.auth.accessToken));
  } catch (error) {
    return apiErrorResponse(error, context);
  }
});
