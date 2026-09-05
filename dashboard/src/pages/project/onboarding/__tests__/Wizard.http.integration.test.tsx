import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import ProjectOnboardingWizard, { type ProjectRootsSource } from "..";
import { browseProjectRoot } from "../projectRootsClient";
import { client } from "../../../../api/client";

const roots: ProjectRootsSource = {
  status: "ready",
  roots: [{ id: "dev", label: "Development", displayPath: "~/dev", readable: true, writable: true }],
};

const success = {
  success: true,
  request_id: "request-1",
  project_id: "widgets",
  workspace_id: "workspace-1",
  source_type: "init",
  root_id: "dev",
  relative_path: "widgets",
  canonical_path: "/srv/dev/widgets",
  default_branch: "main",
  remote_url: null,
  actions: ["registered"],
};

type RequestRecord = { path: string; body: Record<string, unknown> };
let requests: RequestRecord[];
let initAttempts: number;

function response(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });
}

async function requestDetails(input: RequestInfo | URL, init?: RequestInit): Promise<RequestRecord> {
  const request = input instanceof Request ? input : null;
  const url = request ? request.url : String(input);
  const text = typeof init?.body === "string" ? init.body : request ? await request.clone().text() : "";
  return { path: new URL(url, "http://dashboard.test").pathname, body: text ? JSON.parse(text) as Record<string, unknown> : {} };
}

function installHttpServer() {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = await requestDetails(input, init);
    requests.push(request);
    if (request.path === "/api/project/list-roots") {
      return response({ success: true, roots: [{ id: "dev", label: "Development", path: "~/dev", readable: true, writable: true }] });
    }
    if (request.path === "/api/project/browse-root") {
      return response({ success: true, root_id: "dev", relative_path: "", entries: [{ name: "widgets", relative_path: "widgets", is_directory: true, is_git_repository: true, selectable: true }], truncated: false });
    }
    if (request.path === "/api/project/get-github-auth-status") return response({ success: true, installed: true, authenticated: true, host: "github.com", login: "octocat" });
    if (request.path === "/api/project/search-github-repositories") return response({ success: true, repositories: [{ owner: "acme", name: "widgets", full_name: "acme/widgets", visibility: "private", clone_url_https: "https://github.com/acme/widgets.git", clone_url_ssh: "git@github.com:acme/widgets.git", default_branch: "main" }], next_cursor: null });
    if (request.path === "/api/project/onboard") {
      if (request.body.source_mode === "init" && initAttempts++ === 0) {
        return response({ success: false, error_code: "destination_conflict", error: "Destination already exists", phase: "preflight", field_errors: [{ field: "directoryName", message: "Choose another directory" }] }, 422);
      }
      return response({ ...success, source_type: request.body.source_mode === "github_clone" ? "clone" : request.body.source_mode });
    }
    throw new Error(`Unexpected HTTP request: ${request.path}`);
  }));
}

function Harness({ onSuccess = vi.fn() }: { onSuccess?: (result: { project_id: string }) => void }) {
  return <MemoryRouter><ProjectOnboardingWizard open onClose={() => {}} roots={roots} onSuccess={onSuccess} /></MemoryRouter>;
}

async function chooseSource(user: ReturnType<typeof userEvent.setup>, name: RegExp) {
  await user.click(screen.getByRole("radio", { name }));
  await user.click(screen.getByRole("button", { name: "Next" }));
}

async function completeIdentity(user: ReturnType<typeof userEvent.setup>) {
  const name = screen.getByRole("textbox", { name: "Display name" });
  if (!name.getAttribute("value")) await user.type(name, "Widgets");
  const id = screen.getByRole("textbox", { name: "Project ID" });
  if (!id.getAttribute("value")) await user.type(id, "widgets");
  await user.click(screen.getByRole("button", { name: "Next" }));
  await user.click(screen.getByRole("button", { name: "Next" }));
}

beforeEach(() => {
  requests = [];
  initAttempts = 0;
  client.setConfig({ baseUrl: "http://dashboard.test" });
  installHttpServer();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  client.setConfig({ baseUrl: "" });
});

describe("project onboarding wizard against generated-client HTTP commands", () => {
  it("maps the backend browse payload before linking a repository", async () => {
    const result = await browseProjectRoot("dev", "");
    expect(result).toEqual({ relativePath: "", entries: [{ name: "widgets", relativePath: "widgets", isDirectory: true, isGitRepository: true, selectable: true }] });
    expect(requests[0]).toEqual({ path: "/api/project/browse-root", body: { root_id: "dev" } });
  });

  it("drives link through browse and sends the real command shape", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await chooseSource(user, /Existing local repository/);
    await user.selectOptions(screen.getByRole("combobox", { name: "Project root" }), "dev");
    await user.click(await screen.findByText("widgets"));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await completeIdentity(user);
    await user.click(screen.getByRole("button", { name: "Link project" }));
    await waitFor(() => expect(requests.some((request) => request.path === "/api/project/onboard")).toBe(true));
    expect(requests.find((request) => request.path === "/api/project/onboard")?.body).toMatchObject({ source_mode: "link", root_id: "dev", relative_path: "widgets", project_name: "widgets", project_id: "widgets", default_branch: "main" });
  });

  it("preserves init values after the backend's structured retry error", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await chooseSource(user, /New repository/);
    await user.selectOptions(screen.getByRole("combobox", { name: "Project root" }), "dev");
    await user.type(screen.getByRole("textbox", { name: "New directory name" }), "widgets");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await completeIdentity(user);
    await user.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose another directory");
    await user.click(screen.getByRole("button", { name: "Back" }));
    await user.click(screen.getByRole("button", { name: "Back" }));
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByRole("textbox", { name: "New directory name" })).toHaveValue("widgets");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Create project" }));
    const submits = requests.filter((request) => request.path === "/api/project/onboard");
    expect(submits).toHaveLength(2);
    expect(submits[0]?.body).toMatchObject({ source_mode: "init", root_id: "dev", relative_path: "widgets", create_readme: true, create_github: false });
    expect(submits[1]?.body.request_id).toBe(submits[0]?.body.request_id);
  });

  it("drives GitHub clone discovery and preserves an edited destination in the command", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await chooseSource(user, /Clone from GitHub/);
    await user.type(screen.getByRole("textbox", { name: "Search GitHub repositories" }), "widgets");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await user.click(await screen.findByRole("button", { name: /acme\/widgets/ }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Destination root" }), "dev");
    const directory = screen.getByRole("textbox", { name: "Destination directory" });
    await user.clear(directory);
    await user.type(directory, "widgets-copy");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await completeIdentity(user);
    await user.click(screen.getByRole("button", { name: "Clone and add project" }));
    await waitFor(() => expect(requests.some((request) => request.path === "/api/project/onboard")).toBe(true));
    expect(requests.find((request) => request.path === "/api/project/onboard")?.body).toMatchObject({ source_mode: "github_clone", root_id: "dev", relative_path: "widgets-copy", github_repository: { owner: "acme", name: "widgets" } });
    expect(requests.some((request) => request.path === "/api/project/get-github-auth-status")).toBe(true);
    expect(requests.some((request) => request.path === "/api/project/search-github-repositories")).toBe(true);
  });
});
