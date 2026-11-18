import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { EmailDetail } from "./EmailDetail";
import { answer, fakeFetch } from "./test/fetch";
import detailAnswered from "./test/fixtures/detailAnswered";
import detailComplaint from "./test/fixtures/detailComplaint";
import detailEdited from "./test/fixtures/detailEdited";
import detailGroup from "./test/fixtures/detailGroup";
import detailQuote from "./test/fixtures/detailQuote";
import detailSetAside from "./test/fixtures/detailSetAside";
import type { EmailDetail as Detail } from "./types";

function show(email: Detail | ReturnType<typeof answer>) {
  const sent = fakeFetch("status" in email && "body" in email ? email : answer(200, email));
  const onUnauthenticated = vi.fn();
  render(<EmailDetail id={7} onUnauthenticated={onUnauthenticated} />);
  return { sent, onUnauthenticated };
}

function region(name: string) {
  return within(screen.getByRole("region", { name }));
}

describe("EmailDetail", () => {
  it("asks the API for this email", async () => {
    const { sent } = show(detailGroup);
    await screen.findByRole("heading", { level: 1 });
    expect(sent[0]?.url).toBe("/api/emails/7");
  });

  it("marks the escalation summary as written by the model, next to the codes (D81)", async () => {
    show(detailGroup);
    const summary = await screen.findByRole("complementary", { name: "Summary written by the model" });
    expect(summary).toHaveTextContent("Summary written by the model. Check it against the email below.");
    expect(summary).toHaveTextContent(detailGroup.escalation_summary);
    expect(screen.getByText("GROUP")).toBeInTheDocument();
    expect(screen.getByText("DISCOUNT")).toBeInTheDocument();
  });

  it("says when an escalation has no summary", async () => {
    show({ ...detailComplaint, escalation_summary: null });
    const summary = await screen.findByRole("complementary", { name: "Summary written by the model" });
    expect(summary).toHaveTextContent("No summary for this email.");
  });

  it("shows no summary box for an email that is not escalated", async () => {
    show(detailQuote);
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByRole("complementary")).toBeNull();
  });

  it("shows the guest's email as text, never as HTML", async () => {
    show({ ...detailComplaint, clean_body: "<img src=x onerror=alert(1)> <b>bold</b>" });
    const body = (await screen.findByRole("region", { name: "Guest's email" })).querySelector("pre");
    expect(body?.textContent).toBe("<img src=x onerror=alert(1)> <b>bold</b>");
    expect(body?.querySelector("img, b")).toBeNull();
  });

  it("shows the current draft with its version number and author", async () => {
    show(detailEdited);
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByRole("heading", { name: "Draft reply · version 2 (edited by you)" })).toBeInTheDocument();
    const draft = region("Draft reply · version 2 (edited by you)");
    const current = detailEdited.draft.versions[1]?.body ?? "";
    expect(current).toContain("P.S. We look forward to welcoming you.");
    expect(draft.getAllByText((_, node) => node?.tagName === "PRE" && node.textContent === current)).toHaveLength(2); // current + history
  });

  it("keeps every version in the history, oldest first, with who wrote it (D41)", async () => {
    show(detailEdited);
    const user = userEvent.setup();
    await user.click(await screen.findByText("All versions (2)"));
    const items = screen.getAllByRole("listitem");
    expect(items.map((li) => li.querySelector("p")?.textContent.split(",").slice(0, 2).join(","))).toEqual([
      "Version 1, by the agent",
      "Version 2, by you",
    ]);
  });

  it("a draft by the agent alone has no history to open", async () => {
    show(detailQuote);
    await screen.findByRole("heading", { name: /version 1 \(by the agent\)/ });
    expect(screen.queryByText(/All versions/)).toBeNull();
  });

  it("shows each offer: room type, rooms, dates, hold, what changed, and the code-rendered block", async () => {
    show(detailQuote);
    await screen.findByRole("heading", { name: "Offers" });
    const offer = detailQuote.draft.offers[0];
    expect(screen.getByText("Standard Double")).toBeInTheDocument();
    expect(screen.getByText(/2 rooms, 2027-08-16 to 2027-08-21/)).toBeInTheDocument();
    expect(screen.getByText("Not held")).toBeInTheDocument();
    expect(screen.getByText("Differs from the request: lengthened to the minimum stay.")).toBeInTheDocument();
    expect(screen.getByText((_, node) => node?.tagName === "PRE" && node.textContent === offer?.rendered_block)).toBeInTheDocument();
  });

  it("a held offer says so", async () => {
    show(detailAnswered);
    expect(await screen.findByText("Held at approval (48 h)")).toBeInTheDocument();
  });

  it("an approved draft shows its reply waiting in the outbox", async () => {
    show(detailAnswered);
    expect(await screen.findByRole("region", { name: "Reply" })).toHaveTextContent("Waiting to be sent.");
  });

  it("a sent reply shows when", async () => {
    show({
      ...detailAnswered,
      outbox: { ...detailAnswered.outbox, status: "sent", sent_at: "2027-03-01T09:05:00Z" },
    });
    expect(await screen.findByRole("region", { name: "Reply" })).toHaveTextContent(
      "Sent (simulated) 1 Mar 2027, 11:05.",
    );
  });

  it("a version whose checks failed is flagged as needing confirmation (D63, D77)", async () => {
    const versions = detailEdited.draft.versions.map((v) =>
      v.version === 2 ? { ...v, guardrail_results: { ...v.guardrail_results, price_check: false } } : v,
    );
    show({ ...detailEdited, draft: { ...detailEdited.draft, versions } });
    expect(await screen.findByRole("note")).toHaveTextContent(
      "needs confirming before it can be sent: a price block was changed.",
    );
  });

  it("a clean version is not flagged", async () => {
    show(detailEdited);
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("marks a draft past 72 hours", async () => {
    show({ ...detailQuote, draft: { ...detailQuote.draft, past_expiry: true } });
    expect(await screen.findByText("Past 72 h: the next action expires it")).toBeInTheDocument();
  });

  it("a set-aside email has no draft", async () => {
    show(detailSetAside);
    await screen.findByRole("heading", { name: "You have won a free cruise" });
    expect(screen.queryByRole("region", { name: /Draft reply/ })).toBeNull();
    expect(screen.getByText("Set aside")).toBeInTheDocument();
  });

  it("an unknown email says so", async () => {
    show(answer(404, { error: "not_found" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("There is no such email.");
  });

  it("a session that ended goes back to login", async () => {
    const { onUnauthenticated } = show(answer(401, { error: "unauthenticated" }));
    await vi.waitFor(() => {
      expect(onUnauthenticated).toHaveBeenCalledOnce();
    });
  });
});
