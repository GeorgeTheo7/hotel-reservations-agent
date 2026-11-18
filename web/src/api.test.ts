import { describe, expect, it } from "vitest";

import { ApiError, CLIENT_HEADER, NoAnswer, call } from "./api";
import { answer, fakeFetch } from "./test/fetch";

describe("call", () => {
  it("sends the CSRF header and the cookie on a read", async () => {
    const sent = fakeFetch(answer(200, { owner: true }));
    expect(await call("GET", "/session")).toEqual({ owner: true });
    expect(sent).toEqual([
      {
        url: "/api/session",
        method: "GET",
        headers: { [CLIENT_HEADER]: "web" },
        credentials: "same-origin",
        body: undefined,
      },
    ]);
  });

  it("sends a JSON body with its content type, and 204 resolves to undefined", async () => {
    const sent = fakeFetch(answer(204));
    expect(await call("POST", "/session", { password: "p" })).toBeUndefined();
    expect(sent[0]?.headers).toEqual({ [CLIENT_HEADER]: "web", "Content-Type": "application/json" });
    expect(sent[0]?.body).toEqual({ password: "p" });
  });

  it("turns an error answer into ApiError with the API's code", async () => {
    fakeFetch(answer(409, { error: "stale_version", latest: 3 }));
    const error: unknown = await call("POST", "/drafts/1/approve", { version: 2 }).catch(
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, code: "stale_version", body: { latest: 3 } });
  });

  it("reads Retry-After in seconds", async () => {
    fakeFetch(answer(429, { error: "login_locked" }, { "Retry-After": "600" }));
    await expect(call("POST", "/session", { password: "p" })).rejects.toMatchObject({
      status: 429,
      code: "login_locked",
      retryAfterSeconds: 600,
    });
  });

  it("an answer that is not JSON still becomes ApiError, without a code", async () => {
    fakeFetch({ status: 502, body: undefined });
    await expect(call("GET", "/holds")).rejects.toMatchObject({ status: 502, code: undefined });
  });

  it("no answer at all is NoAnswer, not ApiError", async () => {
    fakeFetch(new TypeError("Failed to fetch"));
    await expect(call("GET", "/holds")).rejects.toBeInstanceOf(NoAnswer);
  });
});
