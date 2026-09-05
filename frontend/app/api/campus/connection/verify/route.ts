import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { verifyCampusCredentials } from "@/lib/api/campus";
import { apiErrorResponse, authenticatedRoute } from "@/lib/api/route-utils";

export const POST = authenticatedRoute(
  "/api/campus/connection/verify",
  async (context, request: NextRequest) => {
    try {
      const body = (await request.json()) as { metu_username: string; metu_password: string };
      return NextResponse.json(await verifyCampusCredentials(context.auth.accessToken, body));
    } catch (error) {
      return apiErrorResponse(error, context);
    }
  },
);
