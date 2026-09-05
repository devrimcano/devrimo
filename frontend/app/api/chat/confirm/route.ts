import { NextResponse } from "next/server";
import { continueChatRun, type ChatConfirmation } from "@/lib/api/chat";
import { apiErrorResponse, authenticatedRoute } from "@/lib/api/route-utils";
import { reportServerEvent, tracingHeadersFrom } from "@/lib/posthog-server";
import { OUTCOME_EXPECTED_FAILURE } from "@/lib/telemetry";

export const maxDuration = 600;

export const POST = authenticatedRoute("/api/chat/confirm", async (context, request) => {
  const body = (await request.json()) as {
    confirmation?: ChatConfirmation;
    requirement_id?: string;
    approved?: boolean;
  };
  if (!body.confirmation || !body.requirement_id || typeof body.approved !== "boolean") {
    // A malformed confirmation is the browser and this handler disagreeing
    // about a contract, which is worth counting even though it is a 422 rather
    // than an issue.
    await reportServerEvent("chat_confirmation_rejected", {
      requestId: context.requestId,
      distinctId: context.distinctId,
      sessionId: context.sessionId,
      reason: "invalid_payload",
      outcome: OUTCOME_EXPECTED_FAILURE,
    });
    return NextResponse.json(
      { detail: "Invalid confirmation request", request_id: context.requestId },
      { status: 422 },
    );
  }

  try {
    const continuation = await continueChatRun(
      context.auth.accessToken,
      body.confirmation,
      body.requirement_id,
      body.approved,
      tracingHeadersFrom(request, context.requestId),
    );
    return NextResponse.json(continuation);
  } catch (error) {
    // This used to return a bare 502 and report nothing: a confirmation that
    // never reached the broker looked identical to one the student abandoned.
    return apiErrorResponse(error, context);
  }
});
