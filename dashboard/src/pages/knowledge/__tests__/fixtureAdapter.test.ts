import { describe, expect, it } from "vitest";
import { KnowledgeAdapterError } from "../adapter";
import { createKnowledgeFixtureAdapter } from "../fixtureAdapter";
import { DEFAULT_KNOWLEDGE_FILTERS, type KnowledgeEditDraft } from "../model";

const ANY = { ...DEFAULT_KNOWLEDGE_FILTERS, lifecycle: "" as const };

function draftOf(detail: { title: string; body: string | null; summary: string | null; category: KnowledgeEditDraft["category"]; tags: string[] }): KnowledgeEditDraft {
  return { title: detail.title, body: detail.body ?? "", summary: detail.summary, category: detail.category, tags: detail.tags, reason: "" };
}

describe("knowledge fixture adapter", () => {
  it("lists active records by default and honours every filter", async () => {
    const fixture = createKnowledgeFixtureAdapter();
    const [, , retired, policy] = fixture.recordIds();
    const active = await fixture.list(DEFAULT_KNOWLEDGE_FILTERS, null);
    expect(active.items.map((item) => item.recordId)).not.toContain(retired);
    expect(active.nextCursor).toBeNull();

    const all = await fixture.list(ANY, null);
    expect(all.items.map((item) => item.recordId)).toContain(retired);

    const json = await fixture.list({ ...ANY, query: "JSON equality" }, null);
    expect(json.items.map((item) => item.title)).toEqual(["Scheduler outage 2026-09-28: JSON columns without equality"]);

    const disputed = await fixture.list({ ...ANY, verification: "disputed" }, null);
    expect(disputed.items.map((item) => item.recordId)).toEqual([policy]);
    expect(disputed.items[0]).toMatchObject({ stale: true, lifecycle: "active", authoritative: false });
  });

  it("pages with an opaque cursor", async () => {
    const fixture = createKnowledgeFixtureAdapter({ pageSize: 2 });
    const first = await fixture.list(ANY, null);
    expect(first.items).toHaveLength(2);
    expect(first.nextCursor).not.toBeNull();
    const second = await fixture.list(ANY, first.nextCursor);
    const third = await fixture.list(ANY, second.nextCursor);
    expect(third.nextCursor).toBeNull();
    const ids = [...first.items, ...second.items, ...third.items].map((item) => item.recordId);
    expect(new Set(ids).size).toBe(6);
  });

  it("returns allowed actions per viewer and record", async () => {
    const worker = createKnowledgeFixtureAdapter({ persona: "worker" });
    const supervisor = createKnowledgeFixtureAdapter({ persona: "supervisor" });
    const [postgres, outage, retired] = worker.recordIds();

    expect((await worker.show(postgres!, null)).allowedActions).toEqual(
      expect.arrayContaining(["propose_correction", "history", "link", "create_task"]),
    );
    expect((await worker.show(postgres!, null)).allowedActions).not.toContain("edit");
    expect((await worker.show(outage!, null)).allowedActions).toContain("edit");
    expect((await supervisor.show(postgres!, null)).allowedActions).toEqual(
      expect.arrayContaining(["edit", "retire", "link"]),
    );
    expect((await supervisor.show(retired!, null)).allowedActions).toContain("restore");
    expect((await supervisor.show(retired!, null)).allowedActions).not.toContain("retire");
  });

  it("resolves a pinned revision exactly or not at all", async () => {
    const fixture = createKnowledgeFixtureAdapter();
    const [, outage, , , redacted] = fixture.recordIds();
    const pinned = await fixture.show(outage!, "rev-2-2");
    expect(pinned.viewed).toMatchObject({ sequence: 2, isCurrent: false });
    expect(pinned.current.sequence).toBe(3);
    expect(pinned.summary).toBeNull();

    await expect(fixture.show(outage!, "rev-2-99")).rejects.toMatchObject({ code: "revision_unavailable" });
    await expect(fixture.show("kn-missing", null)).rejects.toBeInstanceOf(KnowledgeAdapterError);

    const tombstone = await fixture.show(redacted!, "rev-5-1");
    expect(tombstone).toMatchObject({ redacted: true, body: null, sources: [] });
  });

  it("guards updates with the observed token, replays by key and refuses protected records", async () => {
    const fixture = createKnowledgeFixtureAdapter();
    const [postgres, outage] = fixture.recordIds();
    const before = await fixture.show(outage!, null);

    const unchanged = await fixture.update({ recordId: outage!, ifRevision: before.current.revisionId, draft: draftOf(before), idempotencyKey: "k-same" });
    expect(unchanged).toEqual({ outcome: "unchanged" });

    const moved = fixture.simulateExternalEdit(outage!);
    const stale = await fixture.update({ recordId: outage!, ifRevision: before.current.revisionId, draft: { ...draftOf(before), title: "Mine" }, idempotencyKey: "k-1" });
    expect(stale).toEqual({ outcome: "conflict", current: moved });

    const landed = await fixture.update({ recordId: outage!, ifRevision: moved.revisionId, draft: { ...draftOf(before), title: "Mine" }, idempotencyKey: "k-1" });
    expect(landed).toMatchObject({ outcome: "updated", revision: { sequence: moved.sequence + 1 } });
    const replay = await fixture.update({ recordId: outage!, ifRevision: "anything", draft: { ...draftOf(before), title: "Other" }, idempotencyKey: "k-1" });
    expect(replay).toEqual({ outcome: "replayed", revision: (landed as { revision: unknown }).revision });
    expect((await fixture.show(outage!, null)).title).toBe("Mine");

    const protectedRecord = await fixture.show(postgres!, null);
    const refused = await fixture.update({ recordId: postgres!, ifRevision: protectedRecord.current.revisionId, draft: { ...draftOf(protectedRecord), title: "X" }, idempotencyKey: "k-2" });
    expect(refused.outcome).toBe("forbidden");
  });

  it("diffs retained bodies and refuses a redacted side", async () => {
    const fixture = createKnowledgeFixtureAdapter();
    const [, outage, , , redacted] = fixture.recordIds();
    const diff = await fixture.diff(outage!, "rev-2-1", "rev-2-2");
    expect(diff.from.sequence).toBe(1);
    expect(diff.blocks.some((block) => block.op === "added" && block.text.includes("## Fix"))).toBe(true);
    await expect(fixture.diff(redacted!, "rev-5-1", "rev-5-2")).rejects.toMatchObject({ code: "revision_redacted" });
  });

  it("serves a task's citations with unreadable endpoints stripped of titles", async () => {
    const fixture = createKnowledgeFixtureAdapter();
    const view = await fixture.taskKnowledge("swift-grove-52");
    expect(view.citations).toHaveLength(3);
    const unauthorized = view.citations.find((citation) => citation.resolution === "unauthorized");
    expect(unauthorized?.title).toBeNull();
    expect(view.citations.find((citation) => !citation.isCurrent && citation.resolution === "pinned")?.revision.sequence).toBe(2);
    expect(await fixture.taskKnowledge("other")).toEqual({ taskId: "other", citations: [], links: [] });
  });

  it("can fail and hold like the dashboard-state fake", async () => {
    const fixture = createKnowledgeFixtureAdapter();
    fixture.failWith(new KnowledgeAdapterError("unavailable", "down"));
    await expect(fixture.list(ANY, null)).rejects.toMatchObject({ code: "unavailable" });
    fixture.failWith(null);

    const release = fixture.hold();
    let settled = false;
    const pending = fixture.list(ANY, null).then(() => { settled = true; });
    await Promise.resolve();
    expect(settled).toBe(false);
    release();
    await pending;
    expect(settled).toBe(true);
    expect(fixture.calls.map((call) => call.op)).toEqual(["list", "list"]);
  });
});
