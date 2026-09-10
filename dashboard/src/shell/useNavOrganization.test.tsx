import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { EMPTY_ORGANIZATION, toggleFolder, type NavOrganization } from "./navOrganization";

const api = vi.hoisted(() => ({
  dashboardStateGet: vi.fn(),
  dashboardStatePut: vi.fn(),
}));

vi.mock("../api/client", () => api);

import { useNavOrganization } from "./useNavOrganization";

function document(value: NavOrganization, revision = 0) {
  return {
    scope: "workspace",
    owner_id: "",
    subject: null,
    revision,
    exists: revision > 0,
    updated_at: revision ? 1 : null,
    namespace: "nav_organization" as const,
    value,
  };
}

beforeEach(() => {
  api.dashboardStateGet.mockReset();
  api.dashboardStatePut.mockReset();
  api.dashboardStateGet.mockResolvedValue({ data: { document: document(EMPTY_ORGANIZATION) } });
});

describe("useNavOrganization", () => {
  it("shows the server default while loading", async () => {
    let resolve!: (value: unknown) => void;
    api.dashboardStateGet.mockReturnValue(new Promise((done) => { resolve = done; }));

    const { result } = renderHook(() => useNavOrganization());
    expect(result.current.status).toBe("loading");
    expect(result.current.organization).toEqual(EMPTY_ORGANIZATION);

    resolve({ data: { document: document(EMPTY_ORGANIZATION) } });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.organization).toEqual(EMPTY_ORGANIZATION);
    expect(api.dashboardStateGet).toHaveBeenCalledWith({
      body: { namespace: "nav_organization" },
      throwOnError: true,
    });
  });

  it("persists an optimistic operation through the shared namespace", async () => {
    const current = document({
      folders: [], assignments: {}, project_order: ["alpha", "beta"],
    }, 4);
    api.dashboardStateGet.mockResolvedValue({ data: { document: current } });
    api.dashboardStatePut.mockImplementation(async ({ body }: { body: { value: NavOrganization; base_revision: number } }) => ({
      data: { document: document(body.value, body.base_revision + 1) },
    }));
    const { result } = renderHook(() => useNavOrganization());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.update((organization) => ({
        ...organization,
        project_order: ["beta", "alpha"],
      }));
    });

    expect(api.dashboardStatePut).toHaveBeenCalledWith({
      body: {
        namespace: "nav_organization",
        base_revision: 4,
        value: { folders: [], assignments: {}, project_order: ["beta", "alpha"] },
      },
      throwOnError: true,
    });
    expect(result.current.organization.project_order).toEqual(["beta", "alpha"]);
  });

  it("rebases a folder operation on a revision conflict", async () => {
    const initial = document({
      folders: [{ id: "work", name: "Work", collapsed: false }],
      assignments: {},
      project_order: [],
    }, 1);
    const concurrent = document({
      folders: [
        { id: "work", name: "Work", collapsed: false },
        { id: "remote", name: "Remote", collapsed: false },
      ],
      assignments: {},
      project_order: [],
    }, 2);
    api.dashboardStateGet.mockResolvedValue({ data: { document: initial } });
    api.dashboardStatePut
      .mockRejectedValueOnce(Object.assign(new Error("API 409"), {
        payload: { error_code: "revision_conflict", current: concurrent },
      }))
      .mockImplementationOnce(async ({ body }: { body: { value: NavOrganization } }) => ({
        data: { document: document(body.value, 3) },
      }));
    const { result } = renderHook(() => useNavOrganization());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.update((organization) => toggleFolder(organization, "work"));
    });

    expect(api.dashboardStatePut).toHaveBeenNthCalledWith(1, expect.objectContaining({
      body: expect.objectContaining({ base_revision: 1 }),
    }));
    expect(api.dashboardStatePut).toHaveBeenNthCalledWith(2, expect.objectContaining({
      body: expect.objectContaining({
        base_revision: 2,
        value: expect.objectContaining({
          folders: expect.arrayContaining([
            expect.objectContaining({ id: "remote" }),
            expect.objectContaining({ id: "work", collapsed: true }),
          ]),
        }),
      }),
    }));
    expect(result.current.organization.folders).toEqual([
      { id: "work", name: "Work", collapsed: true },
      { id: "remote", name: "Remote", collapsed: false },
    ]);
  });
});
