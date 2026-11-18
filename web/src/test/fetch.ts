// Replaces fetch with a queue of canned answers (D84: no server, no network). Each call
// takes the next answer; a call with none left fails the test loudly.
import { vi } from "vitest";

export interface Answer {
  status: number;
  body?: unknown;
  headers?: Record<string, string>;
}

export interface Sent {
  url: string;
  method: string;
  headers: Record<string, string>;
  credentials: RequestCredentials | undefined;
  body: unknown;
}

export function answer(status: number, body?: unknown, headers?: Record<string, string>): Answer {
  return { status, body, headers: headers ?? {} };
}

/** Installs the fake; returns the list of requests sent, in order. */
export function fakeFetch(...answers: (Answer | Error)[]): Sent[] {
  const sent: Sent[] = [];
  const queue = [...answers];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init: RequestInit = {}) => {
      sent.push({
        url,
        method: init.method ?? "GET",
        headers: { ...(init.headers as Record<string, string> | undefined) },
        credentials: init.credentials,
        body: typeof init.body === "string" ? JSON.parse(init.body) : undefined,
      });
      const next = queue.shift();
      if (next === undefined) throw new Error(`unexpected request: ${url}`);
      if (next instanceof Error) return Promise.reject(next);
      const body = next.status === 204 ? null : JSON.stringify(next.body ?? {});
      return Promise.resolve(new Response(body, { status: next.status, headers: next.headers ?? {} }));
    }),
  );
  return sent;
}
