import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";

const api = vi.hoisted(() => {
  vi.resetModules();
  return {
    client: { get: vi.fn() },
    taskSubtasks: vi.fn(),
    taskComments: vi.fn(),
    listAttachmentsApiTasksTaskIdAttachmentsGet: vi.fn(),
  };
});
vi.mock("../../../api/client", () => api);
afterAll(() => {
  vi.doUnmock("../../../api/client");
  vi.resetModules();
});

import { prefetchTaskPane } from "../prefetch";

beforeEach(() => {
  vi.clearAllMocks();
  api.client.get.mockResolvedValue({ data: { task_id: "t1", sessions: [] } });
  api.taskSubtasks.mockResolvedValue({ data: { subtasks: [] } });
  api.taskComments.mockResolvedValue({ data: { comments: [], total: 0 } });
  api.listAttachmentsApiTasksTaskIdAttachmentsGet.mockResolvedValue({ data: { attachments: [] } });
});

describe("prefetchTaskPane", () => {
  it("starts every section read at once, under the keys the sections use", async () => {
    const client = new QueryClient();
    prefetchTaskPane(client, "t1");
    expect(api.client.get).toHaveBeenCalledWith(expect.objectContaining({ url: "/api/tasks/t1/sessions" }));
    expect(api.taskSubtasks).toHaveBeenCalledWith({ body: { task_id: "t1" }, throwOnError: true });
    expect(api.taskComments).toHaveBeenCalledWith({
      body: { task_id: "t1", limit: 50, offset: 0 }, throwOnError: true,
    });
    expect(api.listAttachmentsApiTasksTaskIdAttachmentsGet)
      .toHaveBeenCalledWith({ path: { task_id: "t1" }, throwOnError: true });
    await vi.waitFor(() => expect(client.getQueryData(["task-attachments", "t1"])).toBeDefined());
    for (const key of [["task", "t1", "sessions"], ["task", "t1", "subtasks"], ["task", "t1", "comments", 0]]) {
      expect(client.getQueryData(key)).toBeDefined();
    }
    client.clear();
  });

  it("does not repeat reads the cache already holds fresh", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000 } } });
    prefetchTaskPane(client, "t1");
    await vi.waitFor(() => expect(client.getQueryData(["task-attachments", "t1"])).toBeDefined());
    prefetchTaskPane(client, "t1");
    expect(api.taskSubtasks).toHaveBeenCalledTimes(1);
    expect(api.listAttachmentsApiTasksTaskIdAttachmentsGet).toHaveBeenCalledTimes(1);
    client.clear();
  });
});
