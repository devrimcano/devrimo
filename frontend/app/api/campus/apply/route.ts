import { NextResponse } from "next/server";
import { applyCampusConnection } from "@/lib/api/campus";
import { apiErrorResponse, authenticatedRoute } from "@/lib/api/route-utils";

export const POST = authenticatedRoute("/api/campus/apply", async (context) => {
  try {
    return NextResponse.json(await applyCampusConnection(context.auth.accessToken));
  } catch (error) {
    return apiErrorResponse(error, context);
  }
});
