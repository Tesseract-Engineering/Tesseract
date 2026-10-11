import { describe, expect, it } from "vitest";
import type { AskResponse } from "./api";
import { present, workingLines } from "./present";

const info: AskResponse = {
  verdict: "INFO",
  headline: "Before you design this from scratch: the refund retry pipeline already retries failed refunds.",
  question: "Help me design a refund retry system",
  design_request: { asked_as: "Has anyone already built a refund retry pipeline?" },
  claims: [
    {
      source: "docs",
      title: "Refund retry pipeline",
      quote: "The refund retry pipeline retries failed refunds with idempotency keys.",
      node_id: "doc:payments/refund-retries.md",
      date: "2026-09-01",
      visibility: "internal",
    },
    {
      source: "sharepoint",
      title: "Payments runbook",
      quote: "Refunds that fail three times page the payments on-call.",
      node_id: "sharepoint:payments/runbook.md",
      date: "2026-08-12",
      visibility: "restricted",
      access: "restricted; readable because viewer has sharepoint:payments",
    },
  ],
  owners: [{ role: "owner", name: "Ada Lovelace", source: "doc:payments/refund-retries.md" }],
  proposed_actions: [
    {
      id: "a1",
      type: "email.draft",
      to: "ada@example.com",
      payload: { to: ["ada@example.com"], to_names: ["Ada Lovelace"], subject: "Reusing your work", body: "Hi Ada" },
    },
    {
      id: "a2",
      type: "slack.draft",
      to: "@ada",
      payload: { to: ["@ada"], text: "Hi Ada, can I reuse the refund retry pipeline?" },
    },
    {
      id: "a3",
      type: "agent.launch",
      to: "services/payments",
      payload: { title: "Build on Refund retry pipeline", repo: "services/payments", reviewers: ["Ada Lovelace"] },
    },
  ],
  searched: {
    sources: [
      { source: "docs", documents: 10 },
      { source: "sharepoint", documents: 4 },
    ],
  },
};

const notFound: AskResponse = {
  verdict: "NOT_FOUND",
  headline: "I could not find a grounded answer.",
  reason: "no record covers what the question asks",
  claims: [],
  nearest: [{ title: "Leave policy", source: "docs", node_id: "doc:leave.md", score: 0.4, why: { cover: 0.5 } }],
  restricted_matches: 2,
  restricted_groups: ["sharepoint:security"],
  searched: { sources: [{ source: "docs", documents: 10 }], ask_terms: ["vault", "code"] },
};

describe("present: an INFO answer", () => {
  const v = present(info);

  it("shows the headline, quotes the top records and cites every claim once", () => {
    expect(v.pill.label).toBe("Here's what I found");
    expect(v.lines[0].text).toMatch(/^Before you design this from scratch/);
    expect(v.lines.filter((l) => l.quote)).toHaveLength(2);
    const cited = v.lines.flatMap((l) => l.cites).sort((a, b) => a - b);
    expect(cited).toEqual([1, 2]);
  });

  it("groups sources and labels restricted ones the viewer may read", () => {
    expect(v.groups.map((g) => g.title)).toEqual(["Sources"]);
    expect(v.sources[1].accessLabel).toBe("Restricted · you have access via Payments");
    expect(v.footer).toBe("Checked 2 systems.");
  });

  it("names draft recipients from the draft, else from the address", () => {
    expect(v.actions.map((a) => a.name)).toEqual([
      "Draft an email to Ada Lovelace",
      "Draft a Slack message to Ada",
      "Launch a coding agent: Build on Refund retry pipeline",
    ]);
    expect(v.people).toEqual([expect.objectContaining({ name: "Ada Lovelace", role: "owner", init: "AL" })]);
  });

  it("offers a design diagram for a build request", () => {
    expect(v.design).toEqual({
      question: "Help me design a refund retry system",
      ids: ["doc:payments/refund-retries.md", "sharepoint:payments/runbook.md"],
    });
  });
});

describe("present: NOT_FOUND", () => {
  const v = present(notFound);

  it("shows nearest misses as misses and calls out restricted matches", () => {
    expect(v.pill.label).toBe("I couldn't verify this");
    expect(v.groups[0].title).toBe("Nearest, not an answer");
    expect(v.groups[0].rows[0].muted).toBe(true);
    expect(v.notFound?.hasRestricted).toBe(true);
    expect(v.notFound?.callout).toBe("2 matching documents are restricted (Security) and were not read.");
    expect(v.notFound?.terms).toEqual(["vault", "code"]);
    expect(v.design).toBeNull();
  });

  it("working lines report restricted matches by owner", () => {
    expect(workingLines(["Docs"], notFound)[1]).toBe("Found 2 restricted matches (Security)");
  });
});
