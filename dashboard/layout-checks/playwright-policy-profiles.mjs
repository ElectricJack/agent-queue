// Browser acceptance against the built SPA and an isolated fake daemon.
// Backend archive/handler semantics are covered by test_policy_profiles.py.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
import { loadFixtures, startStubServer } from "./server.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const fixtures = await loadFixtures(join(here, "fixtures"));
const stub = await startStubServer({ distDir: join(here, "..", "dist"), fixtures });
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
stub.override("POST /api/policy/diff", (body) => {
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

let browser;
try {
  browser = await chromium.launch({ executablePath: process.env.CHROME ?? "/usr/bin/google-chrome", headless: true });
  const page = await browser.newPage({ viewport: { width: 1100, height: 900 }, acceptDownloads: true });
  const errors = [];
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
  assert.deepEqual(errors, []);
  assert.deepEqual(stub.unhandled, []);
  console.log("Policy profile browser acceptance passed: upload, defaults, scopes, skip, diff, placeholders, fingerprinted apply, pending review, exact export and download.");
} finally {
  await browser?.close();
  await stub.close();
}
