// Browser acceptance against the built SPA and an isolated fake daemon.
// Backend archive/handler semantics are covered by test_policy_profiles.py.
import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
import { loadFixtures, startStubServer } from "./server.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const fixtures = await loadFixtures(join(here, "fixtures"));
const stub = await startStubServer({ distDir: join(here, "..", "dist"), fixtures });
const evidenceDir = process.env.AQ_POLICY_BROWSER_EVIDENCE_DIR ?? join(tmpdir(), `aq-policy-profiles-${Date.now()}`);
await mkdir(evidenceDir, { recursive: true });
const archive = Buffer.from("policy archive fixture");
const template = "template:project:spec.base";
const router = "playbook:system:router";
const skipped = "template:project:plan.base";
const identical = "template:project:spec.identical";
stub.override("POST /api/policy/export", () => ({
  success: true, checksum: "preview", bundle: {}, archive: archive.toString("base64"),
  files: [{ path: "manifest.json", content: "EXACT MANIFEST" }, { path: "items/0000.json", content: "EXACT POLICY" }],
  written: [],
}));
let previewed;
stub.override("POST /api/policy/diff", (body) => {
  previewed = body;
  const rows = [
    { id: template, name: "spec.base", type: "template", original_scope: "project", status: "will overwrite", state: "copy", current_checksum: "existing", diff: "-previous policy\n+new policy" },
    { id: router, name: "router", type: "playbook", original_scope: "system", status: "new", state: "pending review", current_checksum: null, diff: "" },
    { id: skipped, name: "plan.base", type: "template", original_scope: "project", status: "new", state: "copy", current_checksum: null, diff: "" },
    { id: identical, name: "spec.identical", type: "template", original_scope: "project", status: "identical", state: "copy", current_checksum: "identical", diff: "" },
  ].map((row) => {
    const selection = body.selections?.[row.id];
    return { ...row, scope: selection?.scope ?? "project", destinations: ["fixture/policy/" + row.name],
      requires_scope_choice: row.original_scope === "system",
      selected: selection?.scope !== "skip" && (row.original_scope !== "system" || !!selection)
        && (row.status !== "will overwrite" || !!selection?.overwrite) };
  });
  return { success: true, name: "Browser policy", project_id: body.project_id, items: rows,
    placeholders: [{ name: "branch", description: "Release branch" }], values: { branch: body.values?.branch ?? "" } };
});
let applied;
stub.override("POST /api/policy/apply", (body) => {
  applied = body;
  return { success: true, applied: [template, router], reviews: [{ item_id: router, review_id: "review-1" }], pending_configuration: [], activated: false };
});
stub.override("POST /api/project/list-roots", () => ({
  success: true, roots: [{ id: "dev", label: "Development", path: "/fixture/dev", readable: true, writable: true }],
}));

let browser;
let context;
let page;
let failure;
const errors = [];
try {
  browser = await chromium.launch({ executablePath: process.env.CHROME ?? "/usr/bin/google-chrome", headless: true });
  context = await browser.newContext({ viewport: { width: 1100, height: 900 }, acceptDownloads: true });
  await context.tracing.start({ screenshots: true, snapshots: true });
  page = await context.newPage();
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.goto(stub.url + "/projects/fixture/config");
  await page.getByRole("button", { name: "Import policy", exact: true }).click();
  await page.getByLabel("Policy archive").setInputFiles({ name: "sample.aqpolicy", mimeType: "application/zip", buffer: archive });
  await page.getByText("Browser policy", { exact: true }).waitFor();
  assert.equal(await page.getByRole("group", { name: "template", exact: true }).count(), 1);
  assert.equal(await page.getByRole("group", { name: "playbook", exact: true }).count(), 1);
  assert.equal(await page.getByRole("checkbox", { name: "Overwrite spec.base", exact: true }).isChecked(), false);
  assert.equal(await page.getByRole("checkbox", { name: "Confirm placement for router" }).isChecked(), false);
  assert.equal(await page.getByRole("combobox", { name: "Placement for router" }).inputValue(), "project");
  assert.equal(await page.getByRole("button", { name: "Import selected items" }).isEnabled(), false);
  await page.getByText("Diff for spec.base", { exact: true }).click();
  assert.equal(await page.getByText(/-previous policy/).isVisible(), true);
  await page.getByRole("textbox", { name: "Release branch" }).fill("release");
  await page.getByRole("checkbox", { name: "Overwrite spec.base", exact: true }).check();
  await page.getByRole("combobox", { name: "Placement for router" }).selectOption("global");
  await page.getByRole("combobox", { name: "Placement for plan.base" }).selectOption("skip");
  assert.equal(await page.getByRole("button", { name: "Import selected items" }).isEnabled(), false);
  await page.getByRole("button", { name: "Update preview" }).click();
  await page.getByRole("button", { name: "Import selected items" }).click();
  await page.getByRole("status").filter({ hasText: "Imported 2 items. 1 pending review" }).waitFor();
  assert.equal(applied.project_id, "fixture");
  assert.equal(applied.archive, archive.toString("base64"));
  assert.deepEqual(applied.values, { branch: "release" });
  assert.deepEqual(applied.selections[template], { scope: "project", overwrite: true, expected_checksum: "existing" });
  assert.equal(applied.selections[router].scope, "global");
  assert.equal(applied.selections[skipped].scope, "skip");
  assert.equal(applied.selections[identical].expected_checksum, "identical");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Import policy", exact: true }).click();
  await page.getByRole("combobox", { name: "Policy profile", exact: true }).selectOption("default");
  await page.getByRole("button", { name: "Preview default policy" }).click();
  await page.getByText("Browser policy", { exact: true }).waitFor();
  assert.equal(previewed.builtin, "default");
  assert.equal(previewed.archive, undefined);
  assert.equal(await page.getByRole("combobox", { name: "Placement for router" }).inputValue(), "project");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Export policy", exact: true }).click();
  assert.equal(await page.getByRole("button", { name: "Download .aqpolicy" }).count(), 0);
  await page.getByRole("button", { name: "Preview exact files" }).click();
  await page.getByText("manifest.json", { exact: true }).click();
  assert.equal(await page.getByText("EXACT MANIFEST", { exact: true }).isVisible(), true);
  await page.getByText("items/0000.json", { exact: true }).click();
  assert.equal(await page.getByText("EXACT POLICY", { exact: true }).isVisible(), true);
  const [download] = await Promise.all([
    page.waitForEvent("download"), page.getByRole("button", { name: "Download .aqpolicy" }).click(),
  ]);
  assert.equal(download.suggestedFilename(), "fixture.aqpolicy");
  assert.deepEqual(await readFile(await download.path()), archive);
  await page.screenshot({ path: join(evidenceDir, "export-preview.png"), fullPage: true });
  await page.keyboard.press("Escape");

  // Begin at the rail's Add project control, so policy selection must receive
  // the newly registered project rather than a pre-existing settings page id.
  const projects = [{ id: "fixture", name: "Fixture project" }, { id: "second", name: "Second project" }];
  let repository;
  stub.override("POST /api/project/list", () => ({ projects }));
  stub.override("POST /api/project/get", (body) => ({
    ...projects.find(project => project.id === body.project_id), status: "ACTIVE", repo_default_branch: "main",
  }));
  stub.override("POST /api/project/browse-root", () => ({
    success: true, root_id: "dev", relative_path: "", truncated: false,
    entries: [{ name: repository, relative_path: repository, is_directory: true, is_git_repository: true, selectable: true }],
  }));
  stub.override("POST /api/project/onboard", (body) => {
    projects.push({ id: body.project_id, name: body.project_name });
    stub.override(`GET /api/projects/${body.project_id}/graph`, () => ({ tasks: [], edges: [], gates: [], agents: [] }));
    return {
      success: true, request_id: body.request_id, project_id: body.project_id, workspace_id: `workspace-${body.project_id}`,
      source_type: "link", root_id: body.root_id, relative_path: body.relative_path,
      canonical_path: `/fixture/dev/${body.relative_path}`, default_branch: "main", remote_url: null, actions: ["registered"],
    };
  });
  stub.override("POST /api/task/list", () => ({ tasks: [], total: 0, hidden_completed: 0 }));
  stub.override("POST /api/project/list-workspaces", () => ({ workspaces: [] }));

  for (const profile of ["none", "default", "exported"]) {
    repository = `created-${profile}`;
    const firstRequest = stub.requests.length;
    await page.getByRole("button", { name: "Add project", exact: true }).click();
    await page.getByRole("radio", { name: /Existing local repository/ }).check();
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await page.getByRole("combobox", { name: "Project root" }).selectOption("dev");
    await page.getByRole("treeitem").filter({ hasText: repository }).click();
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await page.getByRole("textbox", { name: "Display name", exact: true }).fill(repository);
    await page.getByRole("textbox", { name: "Project ID", exact: true }).fill(repository);
    assert.equal(await page.getByRole("textbox", { name: "Project ID", exact: true }).inputValue(), repository);
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await page.getByRole("button", { name: "Next", exact: true }).click();
    assert.deepEqual(await page.getByRole("combobox", { name: "Policy profile", exact: true }).locator("option").evaluateAll(options => options.map(option => option.value)), ["none", "default", "exported"]);
    await page.getByRole("combobox", { name: "Policy profile", exact: true }).selectOption(profile);
    await page.screenshot({ path: join(evidenceDir, `onboarding-${profile}-review.png`), fullPage: true });
    await page.getByRole("button", { name: "Link project", exact: true }).click();
    if (profile !== "none") {
      await page.getByRole("dialog", { name: "Import policy profile", exact: true }).waitFor();
      if (profile === "default") {
        await page.getByRole("button", { name: "Preview default policy" }).click();
      } else {
        await page.getByLabel("Policy archive").setInputFiles({ name: "sample.aqpolicy", mimeType: "application/zip", buffer: archive });
      }
      await page.getByText("Browser policy", { exact: true }).waitFor();
      assert.equal(previewed.project_id, repository);
      assert.equal(previewed.builtin, profile === "default" ? "default" : undefined);
      assert.equal(previewed.archive, profile === "exported" ? archive.toString("base64") : undefined);
      assert.equal(await page.getByRole("checkbox", { name: "Overwrite spec.base", exact: true }).isChecked(), false);
      assert.equal(await page.getByRole("checkbox", { name: "Confirm placement for router" }).isChecked(), false);
      assert.equal(await page.getByRole("button", { name: "Import selected items" }).isEnabled(), false);
      await page.screenshot({ path: join(evidenceDir, `onboarding-${profile}-import.png`), fullPage: true });
      // Dismissal retains the created project without applying or rolling back.
      await page.keyboard.press("Escape");
    }
    await page.waitForURL(`**/projects/${repository}/overview`);
    await page.getByRole("heading", { name: repository, exact: true }).waitFor();
    await page.screenshot({ path: join(evidenceDir, `onboarding-${profile}-retained.png`), fullPage: true });
    const journey = stub.requests.slice(firstRequest);
    const creates = journey.filter(request => request.path === "/api/project/onboard");
    assert.equal(creates.length, 1);
    assert.equal(JSON.parse(creates[0].body).project_id, repository);
    assert.equal(journey.some(request => request.path === "/api/policy/apply"), false);
    assert.equal(journey.filter(request => request.path.startsWith("/api/policy/")).length, profile === "none" ? 0 : 1);
    assert.equal(projects.filter(project => project.id === repository).length, 1);
  }
  assert.deepEqual(projects.slice(0, 2), [{ id: "fixture", name: "Fixture project" }, { id: "second", name: "Second project" }]);
  assert.deepEqual(errors, []);
  assert.deepEqual(stub.unhandled, []);
  console.log("Policy profile browser acceptance passed: upload, built-in default, scopes, skip, diff, placeholders, fingerprinted apply, pending review, exact export/download, onboarding with none/default/exported, and retained project navigation after dismissal.");
} catch (error) {
  failure = String(error.stack ?? error);
  await page?.screenshot({ path: join(evidenceDir, "failure.png"), fullPage: true }).catch(() => {});
  throw error;
} finally {
  await writeFile(join(evidenceDir, "requests.json"), JSON.stringify({ requests: stub.requests, unhandled: stub.unhandled, errors, failure }, null, 2));
  await context?.tracing.stop({ path: join(evidenceDir, "trace.zip") });
  console.log(`Browser evidence: ${evidenceDir}`);
  await browser?.close();
  await stub.close();
}
