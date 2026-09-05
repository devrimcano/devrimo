import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { getProfile, patchProfile } from "@/lib/api/profile";
import { apiErrorResponse, authenticatedRoute } from "@/lib/api/route-utils";
import type { ProfileInput } from "@/lib/types";

const ROUTE = "/api/profile";

export const GET = authenticatedRoute(ROUTE, async (context) => {
  try {
    return NextResponse.json(await getProfile(context.auth.accessToken));
  } catch (error) {
    return apiErrorResponse(error, context);
  }
});

export const PATCH = authenticatedRoute(ROUTE, async (context, request: NextRequest) => {
  try {
    const body = (await request.json()) as ProfileInput;
    return NextResponse.json(await patchProfile(context.auth.accessToken, body));
  } catch (error) {
    return apiErrorResponse(error, context);
  }
});
