import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useArchiveTask, useCreateTask, useDeleteTask, useEditTask } from "../hooks";

const api = vi.hoisted(() => {
  // isolate:false lets earlier files cache hooks with a different client.
  // Reload this dependency graph before binding our SDK mock.
  vi.resetModules();
  return { createTask: vi.fn(), editTask: vi.fn(), deleteTask: vi.fn(), archiveTask: vi.fn() };
});
vi.mock("../client", () => api);
const clients: QueryClient[] = [];
beforeEach(() => { for (const fn of Object.values(api)) fn.mockReset().mockResolvedValue({ data: { success: true } }); });
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); });
afterAll(() => {
  // Do not leave hooks bound to this four-method mock for later test files.
  vi.doUnmock("../client");
  vi.resetModules();
});

function setup(hook: typeof useDeleteTask) {
  const client = new QueryClient();
  clients.push(client);
  for (const key of [["projectGraph", "p1"], ["projectGraph", "p2"], ["tasks", "p1"], ["task", "t1"]]) client.setQueryData(key, {});
  return { client, ...renderHook(hook, { wrapper: ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  ) }) };
}

describe("task mutations refresh shared graph snapshots", () => {
  it.each([
    ["create", useCreateTask, { project_id: "p1", title: "New child", parent_id: "t1" }],
    ["edit", useEditTask, { task_id: "t1", title: "Renamed" }],
    ["delete", useDeleteTask, { task_id: "t1" }],
    ["archive", useArchiveTask, { task_id: "t1" }],
  ] as const)("invalidates graphs and task caches after %s", async (_name, hook, input) => {
    const { result, client } = setup(hook as typeof useDeleteTask);
    await act(async () => { await result.current.mutateAsync(input as { task_id: string }); });
    for (const key of [["projectGraph", "p1"], ["projectGraph", "p2"], ["tasks", "p1"], ["task", "t1"]]) {
      expect(client.getQueryState(key)?.isInvalidated, key.join("/")).toBe(true);
    }
  });

  it("leaves graph snapshots intact when a mutation is rejected", async () => {
    api.deleteTask.mockRejectedValue(new Error("active task"));
    const { result, client } = setup(useDeleteTask);
    await act(async () => { await expect(result.current.mutateAsync({ task_id: "t1" })).rejects.toThrow("active task"); });
    expect(client.getQueryState(["projectGraph", "p1"])?.isInvalidated).toBe(false);
  });
});
