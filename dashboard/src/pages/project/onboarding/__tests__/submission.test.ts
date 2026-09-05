import { describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ onboardProject: vi.fn(), getProjectOnboarding: vi.fn() }));
vi.mock("../../../../api/client", () => api);

import { daemonSubmit } from "../submission";

describe("daemon onboarding submission", () => {
  it("reuses its session request id when a failed operation is retried", async () => {
    api.onboardProject.mockResolvedValue({ data: { project_id: "widgets" } });
    const submit = daemonSubmit("3f6d851a-8113-4bcd-bef6-bcae95acb05b");
    const request = {
      mode: "init" as const,
      source: { mode: "init" as const, rootId: "dev", directoryName: "widgets", createReadme: true, createGithub: false, githubOwner: null, githubRepo: "", githubVisibility: "private" as const },
      identity: { projectName: "Widgets", projectId: "widgets", defaultBranch: "main" },
    };
    await submit(request, { onPhase: vi.fn() });
    await submit(request, { onPhase: vi.fn() });
    expect(api.onboardProject).toHaveBeenCalledTimes(2);
    expect(api.onboardProject).toHaveBeenLastCalledWith(expect.objectContaining({ body: expect.objectContaining({ request_id: "3f6d851a-8113-4bcd-bef6-bcae95acb05b", source_mode: "init" }) }));
  });
});
