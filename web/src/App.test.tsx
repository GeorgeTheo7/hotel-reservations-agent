import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { App, COOKIE_NOT_KEPT, SESSION_ENDED } from "./App";
import { answer, fakeFetch } from "./test/fetch";
import detailGroup from "./test/fixtures/detailGroup";
import inbox from "./test/fixtures/inbox";
import inboxSetAside from "./test/fixtures/inboxSetAside";

const UNAUTHENTICATED = answer(401, { error: "unauthenticated" });
const OWNER = answer(200, { owner: true });
const INBOX = answer(200, inbox);

async function logInWith(password: string) {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Owner password"), password);
  await user.click(screen.getByRole("button", { name: "Log in" }));
  return user;
}

describe("App", () => {
  it("shows the login screen when there is no session", async () => {
    fakeFetch(UNAUTHENTICATED);
    render(<App />);
    expect(await screen.findByLabelText("Owner password")).toBeInTheDocument();
  });

  it("goes straight in with a live session, to the inbox", async () => {
    fakeFetch(OWNER, INBOX);
    render(<App />);
    expect(await screen.findByRole("button", { name: "Log out" })).toBeInTheDocument();
    expect(await screen.findByText("Noise during our stay")).toBeInTheDocument();
  });

  it("a session that ends while browsing goes back to login, saying why", async () => {
    fakeFetch(OWNER, UNAUTHENTICATED);
    render(<App />);
    expect(await screen.findByRole("status")).toHaveTextContent(SESSION_ENDED);
    expect(screen.getByLabelText("Owner password")).toBeInTheDocument();
  });

  it("after a login, checks that the browser sent the cookie back before going in", async () => {
    const sent = fakeFetch(UNAUTHENTICATED, answer(204), OWNER, INBOX);
    render(<App />);
    await logInWith("a-good-password");
    expect(await screen.findByRole("button", { name: "Log out" })).toBeInTheDocument();
    expect(sent.map((s) => `${s.method} ${s.url}`)).toEqual([
      "GET /api/session",
      "POST /api/session",
      "GET /api/session",
      "GET /api/emails?limit=50",
    ]);
  });

  it("says so when the password was accepted but the cookie did not come back", async () => {
    fakeFetch(UNAUTHENTICATED, answer(204), UNAUTHENTICATED);
    render(<App />);
    await logInWith("a-good-password");
    expect(await screen.findByRole("status")).toHaveTextContent(COOKIE_NOT_KEPT);
    expect(screen.getByLabelText("Owner password")).toBeInTheDocument();
  });

  it("logs out back to the login screen", async () => {
    const sent = fakeFetch(OWNER, INBOX, answer(204));
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Log out" }));
    expect(await screen.findByLabelText("Owner password")).toBeInTheDocument();
    expect(sent[2]).toMatchObject({ method: "DELETE", url: "/api/session" });
  });

  it("a session that already ended still logs out cleanly", async () => {
    fakeFetch(OWNER, INBOX, UNAUTHENTICATED);
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Log out" }));
    expect(await screen.findByLabelText("Owner password")).toBeInTheDocument();
  });

  it("a subject opens the email; Back to the inbox keeps the filter", async () => {
    const sent = fakeFetch(OWNER, answer(200, inboxSetAside), answer(200, detailGroup), answer(200, inboxSetAside));
    window.history.replaceState(null, "", "/?status=set_aside");
    render(<App />);
    const user = userEvent.setup();
    const spam = inboxSetAside.items[0];
    await user.click(await screen.findByRole("link", { name: spam?.subject ?? "" }));
    expect(window.location.pathname).toBe(`/emails/${String(spam?.id)}`);
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(detailGroup.subject);
    window.history.back();
    expect(await screen.findByLabelText("Show")).toHaveValue("set_aside");
    expect(sent.map((s) => s.url)).toEqual([
      "/api/session",
      "/api/emails?limit=50&status=set_aside",
      `/api/emails/${String(spam?.id)}`,
      "/api/emails?limit=50&status=set_aside",
    ]);
  });

  it("an email can be opened straight from its URL", async () => {
    fakeFetch(OWNER, answer(200, detailGroup));
    window.history.replaceState(null, "", `/emails/${String(detailGroup.id)}`);
    render(<App />);
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(detailGroup.subject);
  });

  it("an unknown path says so, with a way back", async () => {
    fakeFetch(OWNER);
    window.history.replaceState(null, "", "/nowhere");
    render(<App />);
    expect(await screen.findByText(/There is no such page/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to the inbox" })).toHaveAttribute("href", "/");
  });

  it("says when the API does not answer", async () => {
    fakeFetch(new TypeError("Failed to fetch"));
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("did not answer");
  });
});
