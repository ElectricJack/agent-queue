import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { client } from "../../../api/client";
import PolicyProfiles from "../PolicyProfiles";

type Recorded = { path: string; body: Record<string, unknown> };
let requests: Recorded[];
let failAfterConfiguration: boolean;
function response(payload: unknown) { return new Response(JSON.stringify(payload), { headers: { "content-type": "application/json" } }); }

beforeEach(() => {
  requests = [];
  failAfterConfiguration = false;
  client.setConfig({ baseUrl: "http://dashboard.test" });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : null;
    const path = new URL(request?.url ?? String(input), "http://dashboard.test").pathname;
    const text = typeof init?.body === "string" ? init.body : request ? await request.clone().text() : "";
    const body = text ? JSON.parse(text) as Record<string, unknown> : {};
    requests.push({ path, body });
    if (path === "/api/policy/export") return response({ success: true, checksum: "preview", archive: "", bundle: {}, warnings: [{ item_id: "playbook:system:legacy", playbook_id: "legacy", message: "Skipped legacy: source unavailable, recompile and reactivate legacy" }], files: [{ path: "manifest.json", content: "EXACT MANIFEST" }, { path: "items/0000.json", content: "EXACT PAYLOAD" }] });
    if (path === "/api/policy/diff") {
      const selections = body.selections as Record<string, { scope?: string; overwrite?: boolean }> | undefined;
      return response({ success: true, name: "Demo profile", project_id: body.project_id, placeholders: [{ name: "branch", description: "Release branch" }], values: body.values ?? { branch: "" }, items: [
        { id: "template:project:base", name: "base", type: "template", original_scope: "project", scope: selections?.["template:project:base"]?.scope ?? "project", status: "will overwrite", selected: selections?.["template:project:base"]?.overwrite ?? false, requires_scope_choice: false, current_checksum: "existing", diff: "-old policy\n+new policy", state: "copy" },
        { id: "playbook:system:router", name: "router", type: "playbook", original_scope: "system", scope: selections?.["playbook:system:router"]?.scope ?? "project", status: "new", selected: !!selections?.["playbook:system:router"] && selections["playbook:system:router"]?.scope !== "skip", requires_scope_choice: true, current_checksum: null, diff: "", state: "pending review" },
        { id: "integration_settings:project:integration", name: "integration", type: "integration_settings", original_scope: "project", scope: selections?.["integration_settings:project:integration"]?.scope ?? "project", status: "will overwrite", selected: selections?.["integration_settings:project:integration"]?.scope !== "skip" && (selections?.["integration_settings:project:integration"]?.overwrite ?? false), requires_scope_choice: false, current_checksum: "generation-3", diff: "-cadence: 600\n+cadence: 120", state: "configure integration" },
      ] });
    }
    if (path === "/api/policy/apply") {
      const selections = body.selections as Record<string, { scope?: string; overwrite?: boolean }>;
      const configured = selections["integration_settings:project:integration"]?.scope === "project" && selections["integration_settings:project:integration"]?.overwrite === true;
      const applied = Object.keys(selections).filter(id => selections[id]!.scope !== "skip" && (id !== "integration_settings:project:integration" || configured));
      if (failAfterConfiguration) return new Response(JSON.stringify({ success: false,
        error: "File write failed", configured_integration: ["integration_settings:project:integration"],
      }), { status: 422, headers: { "content-type": "application/json" } });
      return response({ success: true, applied, reviews: applied.includes("playbook:system:router") ? [{ item_id: "playbook:system:router", review_id: "review-1" }] : [], pending_configuration: [], activated: false, configured_integration: configured ? ["integration_settings:project:integration"] : [] });
    }
    throw new Error(`Unexpected command ${path}`);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); client.setConfig({ baseUrl: "" }); });

function mount() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}><PolicyProfiles projectId="target" /></QueryClientProvider>);
}

it("uploads, groups types, opens overwrite diff, fills values and explicitly places system items before applying through generated HTTP", async () => {
  const user = userEvent.setup(); mount();
  await user.click(screen.getByRole("button", { name: "Import policy" }));
  await user.upload(screen.getByLabelText("Policy archive"), new File(["zip bytes"], "demo.aqpolicy", { type: "application/zip" }));
  await screen.findByText("Demo profile");
  expect(screen.getByRole("group", { name: "template" })).toBeInTheDocument();
  expect(screen.getByRole("group", { name: "playbook" })).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "Overwrite base" })).not.toBeChecked();
  expect(screen.getByRole("checkbox", { name: "Confirm placement for router" })).not.toBeChecked();
  expect(screen.getByRole("combobox", { name: "Placement for router" })).toHaveValue("project");
  expect(screen.getByRole("button", { name: "Import selected items" })).toBeDisabled();
  await user.click(screen.getByText("Diff for base"));
  expect(screen.getByText(/-old policy/)).toBeVisible();
  await user.type(screen.getByRole("textbox", { name: "Release branch" }), "release");
  await user.click(screen.getByRole("checkbox", { name: "Overwrite base" }));
  await user.selectOptions(screen.getByRole("combobox", { name: "Placement for router" }), "global");
  expect(screen.getByRole("button", { name: "Import selected items" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Update preview" }));
  await screen.findAllByText(/Selected:/);
  await user.click(screen.getByRole("button", { name: "Import selected items" }));
  expect(await screen.findByText(/Imported 2 items. 1 pending review/)).toBeInTheDocument();
  const applied = requests.find(request => request.path === "/api/policy/apply")!.body;
  expect(applied.project_id).toBe("target");
  expect(applied.values).toEqual({ branch: "release" });
  expect(applied.selections).toMatchObject({ "template:project:base": { scope: "project", overwrite: true, expected_checksum: "existing" }, "playbook:system:router": { scope: "global", expected_checksum: null } });
  expect(typeof applied.archive).toBe("string");
  expect(requests.every(request => request.path.startsWith("/api/policy/"))).toBe(true);
  expect(screen.getByRole("status")).toHaveTextContent("No playbooks activated");
});

it("previews every export file before offering the download", async () => {
  const user = userEvent.setup(); mount();
  await user.click(screen.getByRole("button", { name: "Export policy" }));
  expect(screen.queryByRole("button", { name: "Download .aqpolicy" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Preview exact files" }));
  await user.click(await screen.findByText("manifest.json"));
  expect(screen.getByRole("status")).toHaveTextContent("Skipped legacy: source unavailable, recompile and reactivate legacy");
  expect(screen.getByText("EXACT MANIFEST")).toBeVisible();
  await user.click(screen.getByText("items/0000.json"));
  expect(screen.getByText("EXACT PAYLOAD")).toBeVisible();
  expect(screen.getByRole("button", { name: "Download .aqpolicy" })).toBeEnabled();
});

it("loads the built-in default through the same per-item preview and apply flow", async () => {
  const user = userEvent.setup(); mount();
  await user.click(screen.getByRole("button", { name: "Import policy" }));
  await user.selectOptions(screen.getByRole("combobox", { name: "Policy profile" }), "default");
  expect(screen.queryByLabelText("Policy archive")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Preview default policy" }));
  await screen.findByText("Demo profile");
  expect(requests[requests.length - 1]?.body).toEqual({ project_id: "target", builtin: "default" });
  await user.type(screen.getByRole("textbox", { name: "Release branch" }), "main");
  await user.click(screen.getByRole("checkbox", { name: "Overwrite base" }));
  await user.selectOptions(screen.getByRole("combobox", { name: "Placement for router" }), "skip");
  await user.click(screen.getByRole("button", { name: "Update preview" }));
  await user.click(screen.getByRole("button", { name: "Import selected items" }));
  await screen.findByText(/Imported 1 items. 0 pending review/);
  const applied = requests.find(request => request.path === "/api/policy/apply")!.body;
  expect(applied.builtin).toBe("default");
  expect(applied.archive).toBeUndefined();
  expect(applied.selections).toMatchObject({ "playbook:system:router": { scope: "skip" } });
});

it.each([false, true])("previews, deselects and reports integration settings including partial failure %s", async (partialFailure) => {
  failAfterConfiguration = partialFailure;
  const user = userEvent.setup(); mount();
  await user.click(screen.getByRole("button", { name: "Import policy" }));
  await user.upload(screen.getByLabelText("Policy archive"), new File(["zip"], "train.aqpolicy"));
  await screen.findByRole("group", { name: "integration settings" });
  expect(screen.getByRole("checkbox", { name: "Overwrite integration" })).not.toBeChecked();
  await user.click(screen.getByText("Diff for integration"));
  expect(screen.getByText(/-cadence: 600/)).toBeVisible();
  await user.selectOptions(screen.getByRole("combobox", { name: "Placement for integration" }), "skip");
  await user.click(screen.getByRole("button", { name: "Update preview" }));
  expect(requests[requests.length - 1]?.body.selections).toMatchObject({ "integration_settings:project:integration": { scope: "skip" } });
  await user.selectOptions(screen.getByRole("combobox", { name: "Placement for integration" }), "project");
  await user.type(screen.getByRole("textbox", { name: "Release branch" }), "release");
  await user.click(screen.getByRole("checkbox", { name: "Overwrite integration" }));
  await user.click(screen.getByRole("button", { name: "Update preview" }));
  await user.click(screen.getByRole("button", { name: "Import selected items" }));
  if (partialFailure) {
    expect(await screen.findByRole("alert")).toHaveTextContent("1 integration settings already applied");
    expect(screen.getByRole("button", { name: "Import selected items" })).toBeDisabled();
  } else {
    expect(await screen.findByText(/1 integration settings applied/)).toBeInTheDocument();
  }
  const applied = requests.find(request => request.path === "/api/policy/apply")!.body;
  expect(applied.selections).toMatchObject({ "integration_settings:project:integration": { scope: "project", overwrite: true, expected_checksum: "generation-3" } });
});
