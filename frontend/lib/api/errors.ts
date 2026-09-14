/**
 * A failed API call, carrying enough to find the other side of it.
 *
 * `requestId` is the correlation id the browser minted and the broker echoed
 * back. It is what turns "a student says saving failed" into one query that
 * returns the browser event, this app's proxy log and the broker's issue.
 */
export class ApiError extends Error {
  status: number;
  body: unknown;
  requestId: string | null;

  constructor(message: string, status: number, body?: unknown, requestId?: string | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.requestId = requestId ?? null;
  }
}

export function isNotFound(error: unknown) {
  return error instanceof ApiError && error.status === 404;
}

export function isConflict(error: unknown) {
  return error instanceof ApiError && error.status === 409;
}

/** The correlation id of a failure, when the failure knows one. */
export function requestIdOf(error: unknown): string | null {
  return error instanceof ApiError ? error.requestId : null;
}

export type Localize = (values: { tr: string; en: string }) => string;

/**
 * One localized sentence for a failed request.
 *
 * The broker answers in technical English; the student reads Turkish (or
 * English) and needs to know what happened and what to do next, not the
 * internal wording. Keyed by status so the same failure reads the same
 * everywhere, with the network case detected from the fetch rejection.
 */
export function userFacingError(error: unknown, pick: Localize): string {
  const message = error instanceof Error ? error.message : "";
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return pick({ tr: "Oturumun sona ermiş görünüyor. Yeniden giriş yap.", en: "Your session looks expired. Please sign in again." });
    }
    if (error.status === 403) {
      return pick({ tr: "Bu işlem için yetkin yok.", en: "You do not have permission for this." });
    }
    if (error.status === 404) {
      return pick({ tr: "Aradığımız kayıt bulunamadı.", en: "We could not find what was requested." });
    }
    if (error.status === 409) {
      return pick({ tr: "Bu işlem şu anki durumla çakıştı. Sayfayı yenileyip tekrar dene.", en: "That conflicts with the current state. Refresh and try again." });
    }
    if (error.status === 422) {
      return pick({ tr: "Gönderilen bilgi kabul edilmedi. Girdiğin değerleri kontrol edip tekrar dene.", en: "The server rejected that input. Check the values and try again." });
    }
    if (error.status === 429) {
      return pick({ tr: "Çok fazla istek gönderildi. Kısa bir mola verip tekrar dene.", en: "Too many requests. Take a short pause and try again." });
    }
    if (error.status >= 500) {
      return pick({ tr: "Sunucuda bir sorun oldu. Birkaç saniye sonra tekrar dene.", en: "Something went wrong on the server. Try again in a moment." });
    }
  }
  if (/failed to fetch|networkerror|load failed|network/i.test(message)) {
    return pick({ tr: "Sunucuya ulaşılamadı. İnternetini kontrol edip tekrar dene.", en: "Could not reach the server. Check your connection and try again." });
  }
  return pick({ tr: "İşlem tamamlanamadı. Lütfen tekrar dene.", en: "That did not go through. Please try again." });
}
