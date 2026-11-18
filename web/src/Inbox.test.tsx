import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Inbox } from "./Inbox";
import { hotelTime } from "./emails";
import inbox from "./test/fixtures/inbox";
import inboxLater from "./test/fixtures/inboxLater";
import inboxPage1 from "./test/fixtures/inboxPage1";
import inboxPage2 from "./test/fixtures/inboxPage2";
import inboxSetAside from "./test/fixtures/inboxSetAside";
import { answer, fakeFetch } from "./test/fetch";

function show(pageSize?: number) {
  const onUnauthenticated = vi.fn();
  render(<Inbox onUnauthenticated={onUnauthenticated} pageSize={pageSize ?? 50} />);
  return { onUnauthenticated, user: userEvent.setup() };
}

async function rows() {
  return within(await screen.findByRole("list", { name: "Emails" })).getAllByRole("listitem");
}

function row(subject: string) {
  const found = screen.getByText(subject).closest("li");
  if (found === null) throw new Error(`no row for ${subject}`);
  return within(found);
}

describe("Inbox", () => {
  it("lists every email but set-aside ones, newest first, as the API orders them", async () => {
    const sent = fakeFetch(answer(200, inbox));
    show();
    expect((await rows()).map((li) => li.querySelector(".subject")?.textContent)).toEqual(
      inbox.items.map((e) => e.subject),
    );
    expect(sent[0]?.url).toBe("/api/emails?limit=50");
  });

  it("shows status, reason codes and the guest's address on each row", async () => {
    fakeFetch(answer(200, inbox));
    show();
    await rows();
    const group = row("Δωμάτια για βάπτιση");
    expect(group.getByText("Escalated")).toBeInTheDocument();
    expect(group.getByText("GROUP")).toBeInTheDocument();
    expect(group.getByText("DISCOUNT")).toBeInTheDocument();
    expect(group.getByText("eleni@example.test")).toBeInTheDocument();
    expect(row("Family stay").getByText("Awaiting approval")).toBeInTheDocument();
    expect(row("Sea view room in June").getByText("Answered")).toBeInTheDocument();
    expect(row("November break").getByText("Closed")).toBeInTheDocument();
  });

  it("shows the time in hotel time (Europe/Athens, D23)", async () => {
    fakeFetch(answer(200, inbox));
    show();
    await rows();
    const noise = inbox.items.find((e) => e.subject === "Noise during our stay");
    expect(noise?.received_at).toBe("2026-11-16T07:45:00Z");
    expect(hotelTime("2026-11-16T07:45:00Z")).toBe("16 Nov 2026, 09:45"); // UTC+2 in November
    expect(row("Noise during our stay").getByText("16 Nov 2026, 09:45")).toBeInTheDocument();
  });

  it("marks only the pending drafts past 72 hours", async () => {
    fakeFetch(answer(200, inboxLater));
    show();
    await rows();
    const marked = screen.getAllByText("Past 72 h").map((b) => b.closest("li")?.querySelector(".subject")?.textContent);
    const pending = inboxLater.items.filter((e) => e.draft_past_expiry).map((e) => e.subject);
    expect(pending).toHaveLength(3);
    expect(marked).toEqual(pending);
    expect(row("Sea view room in June").queryByText("Past 72 h")).toBeNull(); // answered
  });

  it("no mark before 72 hours", async () => {
    fakeFetch(answer(200, inbox));
    show();
    await rows();
    expect(screen.queryByText("Past 72 h")).toBeNull();
  });

  it("the set-aside filter asks the API for set-aside emails only (D50)", async () => {
    const sent = fakeFetch(answer(200, inbox), answer(200, inboxSetAside));
    const { user } = show();
    await rows();
    await user.selectOptions(screen.getByLabelText("Show"), "Set aside");
    expect(await screen.findByText("You have won a free cruise")).toBeInTheDocument();
    expect(await rows()).toHaveLength(1);
    expect(sent[1]?.url).toBe("/api/emails?limit=50&status=set_aside");
  });

  it("loads the next page after the last row, using the API's cursor", async () => {
    const sent = fakeFetch(answer(200, inboxPage1), answer(200, inboxPage2));
    const { user } = show(4);
    expect(await rows()).toHaveLength(4);
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("Sea view room in June");
    expect((await rows()).map((li) => li.querySelector(".subject")?.textContent)).toEqual([
      ...inboxPage1.items.map((e) => e.subject),
      ...inboxPage2.items.map((e) => e.subject),
    ]);
    const cursor = encodeURIComponent(inboxPage1.next_cursor);
    expect(sent[1]?.url).toBe(`/api/emails?limit=4&cursor=${cursor}`);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull(); // last page
  });

  it("an empty filter says so", async () => {
    fakeFetch(answer(200, inbox), answer(200, { items: [], next_cursor: null }));
    const { user } = show();
    await rows();
    await user.selectOptions(screen.getByLabelText("Show"), "Expired");
    expect(await screen.findByText("No emails with this status.")).toBeInTheDocument();
  });

  it("a session that ended goes back to the login screen", async () => {
    fakeFetch(answer(401, { error: "unauthenticated" }));
    const { onUnauthenticated } = show();
    await vi.waitFor(() => {
      expect(onUnauthenticated).toHaveBeenCalledOnce();
    });
  });

  it("another failure can be retried", async () => {
    fakeFetch(answer(500, { error: "internal" }), answer(200, inbox));
    const { user } = show();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the inbox (HTTP 500).");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await rows()).toHaveLength(inbox.items.length);
  });
});
