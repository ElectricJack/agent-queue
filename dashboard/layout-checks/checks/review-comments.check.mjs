import assert from "node:assert/strict";
import { REVIEW, SECTION, QUOTE } from "../fixtures/review-comments.mjs";

export const name = "review-comments";

async function settle(page) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function scrollToSection(page) {
  return page.evaluate((section) => {
    const body = document.querySelector("[data-review-body]");
    const sectionHeading = [...body.querySelectorAll("h2")].find((h) => h.querySelector("span").textContent === section);
    const heading = sectionHeading.nextElementSibling;
    body.scrollTop += heading.getBoundingClientRect().top - body.getBoundingClientRect().top - 8;
    return body.scrollTop;
  }, SECTION);
}

async function scrollToQuote(page) {
  return page.evaluate((quote) => {
    const body = document.querySelector("[data-review-body]");
    const paragraph = [...body.querySelectorAll("p")].find((p) => p.textContent === quote);
    body.scrollTop += paragraph.getBoundingClientRect().top - body.getBoundingClientRect().top - 4;
    return body.scrollTop;
  }, QUOTE);
}

async function openSection(page) {
  const button = await page.$('[data-review-body] h3 button');
  await button.evaluate((el) => el.style.visibility = "visible");
  // A real pointer click, without Puppeteer's implicit scrollIntoView altering the scroll under test.
  const point = await button.boundingBox();
  await page.mouse.click(point.x + point.width / 2, point.y + point.height / 2);
  await page.waitForSelector("#review-comment-body");
  await settle(page);
}

async function selectQuote(page) {
  await page.evaluate((quote) => {
    const body = document.querySelector("[data-review-body]");
    const paragraph = [...body.querySelectorAll("p")].find((p) => p.textContent === quote);
    const range = document.createRange();
    range.selectNodeContents(paragraph);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    paragraph.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  }, QUOTE);
  await page.waitForSelector('body > button::-p-text(Comment)');
  await settle(page);
  const button = await page.$('body > button::-p-text(Comment)');
  const point = await button.boundingBox();
  const g = await page.evaluate(() => {
    const body = document.querySelector("[data-review-body]");
    return { top: body.getBoundingClientRect().top, bottom: body.getBoundingClientRect().bottom, scrollTop: body.scrollTop };
  });
  assert.ok(point.y >= Math.max(0, g.top) &&
    point.y + point.height <= Math.min(g.bottom, await page.evaluate(() => innerHeight)) + 1,
  "selection action is outside the visible document");
  await page.mouse.click(point.x + point.width / 2, point.y + point.height / 2);
  await page.waitForSelector("#review-comment-body");
  await settle(page);
  return g.scrollTop;
}

async function assertDraft(page, body) {
  assert.equal(await page.$eval("#review-comment-body", (el) => el.value), body);
  assert.equal(await page.evaluate(() => document.querySelector("#review-comment-body") === window.commentEditor), true, "repositioning recreated the editor");
}

async function geometry(page) {
  return page.evaluate((quote) => {
    const box = (el) => {
      const r = el.getBoundingClientRect();
      return { top: r.top, bottom: r.bottom, left: r.left, right: r.right };
    };
    const body = document.querySelector("[data-review-body]");
    const editor = document.querySelector("#review-comment-body").parentElement;
    const paragraph = [...body.querySelectorAll("p")].find((p) => p.textContent === quote);
    return {
      editor: box(editor), paragraph: box(paragraph), body: box(body),
      scrollTop: body.scrollTop, windowY: window.scrollY,
      width: window.innerWidth, height: window.innerHeight,
      focus: document.activeElement?.id,
    };
  }, QUOTE);
}

function visibleContext(g, scrollTop) {
  assert.ok(Math.abs(g.scrollTop - scrollTop) <= 1, `opening changed document scroll: ${scrollTop} → ${g.scrollTop}`);
  assert.equal(g.focus, "review-comment-body");
  assert.ok(g.editor.left >= 0 && g.editor.right <= g.width + 1, "editor exceeds viewport width");
  assert.ok(g.editor.top >= 0 && g.editor.bottom <= g.height + 1, "editor exceeds viewport height");
  // A short landscape pane can show only part of the paragraph before opening.
  // Preserve that visible context as well as the entire paragraph when it fits.
  const visibleTop = Math.max(0, g.body.top, g.paragraph.top);
  const visibleBottom = Math.min(g.height, g.body.bottom, g.paragraph.bottom);
  assert.ok(visibleBottom > visibleTop, "referenced paragraph is not visible");
  const overlap = g.editor.left < g.paragraph.right && g.editor.right > g.paragraph.left &&
    g.editor.top < visibleBottom && g.editor.bottom > visibleTop;
  assert.equal(overlap, false, "editor covers the referenced paragraph despite space below it");
}

export async function run(t) {
  await t.page.goto(t.url(`/reviews/${REVIEW}`), { waitUntil: "networkidle0" });
  await t.page.waitForSelector('[data-review-body] h3');
  // A transformed scrolling ancestor would contain a non-portaled fixed editor.
  await t.page.$eval('[data-review-body]', (el) => el.style.transform = "translateZ(0)");
  const scrollTop = await scrollToSection(t.page);
  assert.ok(scrollTop > 1000, "fixture is not scrolled far below the top");
  await settle(t.page);
  await t.shot("scrolled-before");
  await openSection(t.page);
  visibleContext(await geometry(t.page), scrollTop);
  await t.shot("section-editor");
  await t.page.type("#review-comment-body", "Clarify these details.");
  await t.page.evaluate(() => { window.commentEditor = document.querySelector("#review-comment-body"); });
  await t.page.keyboard.down("Shift");
  await t.page.keyboard.press("Tab");
  await t.page.keyboard.up("Shift");
  assert.equal(await t.page.evaluate(() => document.activeElement.textContent.trim()), "Submit");
  await t.page.keyboard.press("Tab");
  assert.equal(await t.page.evaluate(() => document.activeElement.id), "review-comment-body");

  if (t.page.viewport().width >= 1200) {
    await t.page.$eval('[data-review-body]', (el) => { el.parentElement.parentElement.style.width = "480px"; });
    await settle(t.page);
    const narrowScroll = await scrollToQuote(t.page);
    await settle(t.page);
    visibleContext(await geometry(t.page), narrowScroll);
    await assertDraft(t.page, "Clarify these details.");
    assert.ok(await t.page.$eval('[data-review-body]', (el) => el.clientWidth >= 440), "companions squeezed a narrow pane on a wide window");
    await t.shot("narrow-pane-draft");
    await t.page.$eval('[data-review-body]', (el) => { el.parentElement.parentElement.style.width = ""; });
    await settle(t.page);
    await scrollToSection(t.page);
    await settle(t.page);
  }

  const movedScroll = await t.page.$eval('[data-review-body]', (el) => { el.scrollTop += 20; return el.scrollTop; });
  await settle(t.page);
  visibleContext(await geometry(t.page), movedScroll);
  await assertDraft(t.page, "Clarify these details.");

  // Create actual browser scrolling in addition to the pane's internal scrollbar.
  await t.page.evaluate(() => {
    document.body.style.height = `${innerHeight + 300}px`;
    document.documentElement.style.overflowY = "auto";
    window.scrollTo(0, 24);
  });
  await settle(t.page);
  const scrolled = await geometry(t.page);
  assert.equal(scrolled.windowY, 24);
  visibleContext(scrolled, movedScroll);
  await assertDraft(t.page, "Clarify these details.");
  await t.shot("browser-scroll");

  const viewport = t.page.viewport();
  await t.page.setViewport({ ...viewport, height: viewport.height - 32 });
  await settle(t.page);
  await scrollToQuote(t.page);
  await settle(t.page);
  const resized = await geometry(t.page);
  visibleContext(resized, resized.scrollTop);
  await assertDraft(t.page, "Clarify these details.");
  await t.shot("resized-draft");
  await t.page.keyboard.press("Escape");
  await t.page.waitForFunction(() => !document.querySelector("#review-comment-body"));
  assert.equal(await t.page.evaluate(() => document.activeElement.textContent.trim()), "Comment on this section");
  assert.equal(await t.page.$eval('[data-review-body]', (el) => el.scrollTop), resized.scrollTop);
  await t.page.setViewport(viewport);
  await scrollToSection(t.page);
  await settle(t.page);
  const reopenScroll = await t.page.$eval('[data-review-body]', (el) => el.scrollTop);
  await openSection(t.page);
  const reopened = await geometry(t.page);
  assert.equal(reopened.windowY, 24, "opening moved the browser scrollbar");
  visibleContext(reopened, reopenScroll);
  await t.page.type("#review-comment-body", "Clarify these details.");
  const submit = await t.page.$('button::-p-text(Submit)');
  await submit.click();
  await t.page.waitForFunction(() => !document.querySelector("#review-comment-body"));
  const comments = t.stub.requests.filter((r) => r.path === "/api/review/comment");
  assert.deepEqual(JSON.parse(comments.at(-1).body), {
    review_id: REVIEW, revision: 2, heading_path: [SECTION, "Details"], quote: null,
    body: "Clarify these details.",
  });

  const selectionScroll = await selectQuote(t.page);
  visibleContext(await geometry(t.page), selectionScroll);
  await t.shot("selection-editor");
  await t.page.type("#review-comment-body", "Comment on this exact quote.");
  await t.page.click('button::-p-text(Submit)');
  await t.page.waitForFunction(() => !document.querySelector("#review-comment-body"));
  const last = t.stub.requests.filter((r) => r.path === "/api/review/comment").at(-1);
  assert.deepEqual(JSON.parse(last.body), {
    review_id: REVIEW, revision: 2, heading_path: [SECTION, "Details"], quote: QUOTE,
    body: "Comment on this exact quote.",
  });

  await openSection(t.page);
  await t.page.mouse.click(2, 2);
  await t.page.waitForFunction(() => !document.querySelector("#review-comment-body"));

  if (t.page.viewport().height >= 500) {
    const h2Scroll = await t.page.evaluate((section) => {
      const body = document.querySelector("[data-review-body]");
      const heading = [...body.querySelectorAll("h2")].find((h) => h.querySelector("span").textContent === section);
      body.scrollTop += heading.getBoundingClientRect().top - body.getBoundingClientRect().top - 8;
      heading.querySelector("button").style.visibility = "visible";
      return body.scrollTop;
    }, SECTION);
    await settle(t.page);
    const headingButton = await t.page.$('[data-review-body] #section-18 button');
    const point = await headingButton.boundingBox();
    await t.page.mouse.click(point.x + point.width / 2, point.y + point.height / 2);
    await t.page.waitForSelector("#review-comment-body");
    await settle(t.page);
    visibleContext(await geometry(t.page), h2Scroll);
    await t.shot("parent-section-editor");
    await t.page.keyboard.press("Escape");
  }
}
