// The one place the UI talks to the API. Every request goes to /api on the page's own
// origin (D83), sends the session cookie (same origin only) and the header the CSRF
// guard requires on every change (D82).

export const CLIENT_HEADER = "X-Anemos-Client";
export const CLIENT_VALUE = "web";

type Method = "GET" | "POST" | "DELETE";

/** An answer that is not 2xx. `code` is the API's `{"error": ...}`, when it sent one. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | undefined;
  readonly body: unknown;
  /** From Retry-After (the login lock's 429), in seconds. */
  readonly retryAfterSeconds: number | undefined;

  constructor(status: number, body: unknown, retryAfter: string | null) {
    const code = errorCode(body);
    super(`HTTP ${String(status)}${code === undefined ? "" : `: ${code}`}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.body = body;
    const seconds = retryAfter === null ? Number.NaN : Number(retryAfter);
    this.retryAfterSeconds = Number.isFinite(seconds) ? seconds : undefined;
  }
}

/** The request never got an answer (the API is not running, the network is down). */
export class NoAnswer extends Error {
  constructor(cause: unknown) {
    super("the API did not answer", { cause });
    this.name = "NoAnswer";
  }
}

function errorCode(body: unknown): string | undefined {
  if (typeof body === "object" && body !== null && "error" in body) {
    const { error } = body;
    return typeof error === "string" ? error : undefined;
  }
  return undefined;
}

/** Sends one request; resolves with the parsed JSON body (undefined for 204). */
export async function call(method: Method, path: string, body?: unknown): Promise<unknown> {
  const headers: Record<string, string> = { [CLIENT_HEADER]: CLIENT_VALUE };
  const init: RequestInit = { method, headers, credentials: "same-origin" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response: Response;
  try {
    response = await fetch(`/api${path}`, init);
  } catch (error) {
    throw new NoAnswer(error);
  }
  const parsed: unknown =
    response.status === 204 ? undefined : await response.json().catch(() => undefined);
  if (!response.ok) {
    throw new ApiError(response.status, parsed, response.headers.get("Retry-After"));
  }
  return parsed;
}
