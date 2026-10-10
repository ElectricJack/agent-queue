import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { client } from "../../../api/client";
import PolicyProfiles from "../PolicyProfiles";

type Recorded = { path: string; body: Record<string, unknown> };
let requests: Recorded[];
function response(payload: unknown) { return new Response(JSON.stringify(payload), { headers: { "content-type": "application/json" } }); }

beforeEach(() => {
  requests = [];
  client.setConfig({ baseUrl: "http://dashboard.test" });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : null;
    const path = new URL(request?.url ?? String(input), "http://dashboard.test").pathname;
    const text = typeof init?.body === "string" ? init.body : request ? await request.clone().text() : "";
    const body = text ? JSON.parse(text) as Record<string, unknown> : {};
    requests.push({ path, body });
    if (path === "/api/policy/export") return response({ success: true, checksum: "preview", archive: "", bundle: {}, files: [{ path: "manifest.json", content: "EXACT MANIFEST" }, { path: "items/0000.json", content: "EXACT PAYLOAD" }] });
    if (path === "/api/policy/diff") {
      const selections = body.selections as Record<string, { scope?: string; overwrite?: boolean }> | undefined;
      return response({ success: true, name: "Demo profile", project_id: "target", placeholders: [{ name: "branch", description: "Release branch" }], values: { branch: "" }, items: [
        { id: "template:project:base", name: "base", type: "template", original_scope: "project", scope: selections?.["template:project:base"]?.scope ?? "project", status: "will overwrite", selected: selections?.["template:project:base"]?.overwrite ?? false, requires_scope_choice: false, current_checksum: "existing", diff: "-old policy\n+new policy", state: "copy" },
        { id: "playbook:system:router", name: "router", type: "playbook", original_scope: "system", scope: selections?.["playbook:system:router"]?.scope ?? "project", status: "new", selected: !!selections?.["playbook:system:router"] && selections["playbook:system:router"]?.scope !== "skip", requires_scope_choice: true, current_checksum: null, diff: "", state: "pending review" },
      ] });
    }
    if (path === "/api/policy/apply") return response({ success: true, applied: ["template:project:base", "playbook:system:router"], reviews: [{ item_id: "playbook:system:router", review_id: "review-1" }], pending_configuration: [] });
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
  expect(applied.values).toEqual({ branch: "release" });
  expect(applied.selections).toMatchObject({ "template:project:base": { scope: "project", overwrite: true, expected_checksum: "existing" }, "playbook:system:router": { scope: "global", expected_checksum: null } });
  expect(typeof applied.archive).toBe("string");
});

it("previews every export file before offering the download", async () => {
  const user = userEvent.setup(); mount();
  await user.click(screen.getByRole("button", { name: "Export policy" }));
  expect(screen.queryByRole("button", { name: "Download .aqpolicy" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Preview exact files" }));
  await user.click(await screen.findByText("manifest.json"));
  expect(screen.getByText("EXACT MANIFEST")).toBeVisible();
  await user.click(screen.getByText("items/0000.json"));
  expect(screen.getByText("EXACT PAYLOAD")).toBeVisible();
  expect(screen.getByRole("button", { name: "Download .aqpolicy" })).toBeEnabled();
});
