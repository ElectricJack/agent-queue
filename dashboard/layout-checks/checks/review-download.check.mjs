// Exercise native browser downloads from the built Reviews reader, with no live daemon.
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { REVIEW_ID, REVIEW_MARKDOWN } from "../fixtures/reviews.mjs";

export const name = "review-download";
export const profiles = ["phone-390", "desktop"];

export async function run(t) {
  const downloadDir = await mkdtemp(join(tmpdir(), "aq-review-download-"));
  const context = t.page.browserContext();
  await context.setDownloadBehavior({ policy: "allow", downloadPath: downloadDir });
  try {
    await t.page.goto(t.url(`/reviews/${REVIEW_ID}`), { waitUntil: "networkidle0" });
    const button = '::-p-aria([name="Download Markdown"][role="button"])';
    await t.page.waitForSelector(button);

    async function download(revision, keyboard = false) {
      const requestsBefore = t.stub.requests.length;
      if (keyboard) {
        await t.page.$eval(button, (el) => el.focus());
        await t.page.keyboard.press("Enter");
      } else {
        await t.page.click(button);
      }
      const filename = `Café-plan- draft-rev-${revision}.md`;
      const file = join(downloadDir, filename);
      let bytes;
      const deadline = Date.now() + 10_000;
      while (Date.now() < deadline) {
        try {
          bytes = await readFile(file);
          break;
        } catch (error) {
          if (error.code !== "ENOENT") throw error;
          await delay(50);
        }
      }
      assert.ok(bytes, `no native download named ${filename}`);
      assert.deepEqual(bytes, Buffer.from(REVIEW_MARKDOWN[revision], "utf8"), "download changed raw Markdown bytes");
      assert.equal(t.stub.requests.length, requestsBefore, "download unnecessarily requested server content");
      assert.equal(await t.page.$('a[download]'), null, "temporary download link leaked");
    }

    // A diff view still exports the selected revision's entire raw document.
    await t.page.click('input[type="checkbox"]');
    await t.page.waitForFunction(() => document.body.innerText.includes("Diff text is not the document"));
    await download(2, true);
    // Selecting history must use rev 1's content and filename, even though rev 2 is current.
    await t.page.select('[aria-label="Review revision"]', "1");
    await t.page.waitForFunction(() => document.body.innerText.includes("Historical café 日本語"));
    await download(1);
    assert.equal(t.stub.requests.filter((r) => /\/api\/review\/(decide|comment|import-edits)$/.test(r.path)).length, 0);
  } finally {
    await context.setDownloadBehavior({ policy: "default" });
    await rm(downloadDir, { recursive: true, force: true });
  }
}
