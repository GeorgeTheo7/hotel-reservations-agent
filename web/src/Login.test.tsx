import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError, NoAnswer } from "./api";
import { Login, loginFailure } from "./Login";
import { answer, fakeFetch } from "./test/fetch";

function setup() {
  const onLoggedIn = vi.fn();
  render(<Login onLoggedIn={onLoggedIn} />);
  return { onLoggedIn, user: userEvent.setup() };
}

describe("Login", () => {
  it("sends the password once and reports success", async () => {
    const sent = fakeFetch(answer(204));
    const { onLoggedIn, user } = setup();
    await user.type(screen.getByLabelText("Owner password"), "a-good-password");
    await user.click(screen.getByRole("button", { name: "Log in" }));
    expect(onLoggedIn).toHaveBeenCalledOnce();
    expect(sent).toHaveLength(1);
    expect(sent[0]?.body).toEqual({ password: "a-good-password" });
  });

  it("cannot be sent empty", () => {
    fakeFetch();
    setup();
    expect(screen.getByRole("button", { name: "Log in" })).toBeDisabled();
  });

  it("a wrong password is said plainly and the owner can try again", async () => {
    fakeFetch(answer(401, { error: "wrong_password" }));
    const { onLoggedIn, user } = setup();
    await user.type(screen.getByLabelText("Owner password"), "not-it");
    await user.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Wrong password.");
    expect(onLoggedIn).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Log in" })).toBeEnabled();
  });

  it("the lock says how long to wait", async () => {
    fakeFetch(answer(429, { error: "login_locked" }, { "Retry-After": "600" }));
    const { user } = setup();
    await user.type(screen.getByLabelText("Owner password"), "whatever");
    await user.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Try again in 10 minutes.");
  });

  it("is a password field the browser can fill", () => {
    fakeFetch();
    setup();
    const field = screen.getByLabelText("Owner password");
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "current-password");
  });
});

describe("loginFailure", () => {
  it.each([
    [new ApiError(429, { error: "login_locked" }, "1"), "Try again in 1 minute."],
    [new ApiError(429, { error: "login_locked" }, "61"), "Try again in 2 minutes."],
    [new ApiError(429, { error: "login_locked" }, null), "Try again in 1 minute."],
    [new ApiError(403, { error: "csrf" }, null), "Could not log in (HTTP 403)."],
    [new NoAnswer(new TypeError("x")), "The API did not answer. Is it running on 127.0.0.1:8000?"],
  ])("%o → %s", (error, message) => {
    expect(loginFailure(error)).toContain(message);
  });
});
