import assert from "node:assert/strict";

/**
 * Horizontal overflow. `scroll` is how far the document scrolls sideways.
 * `offenders` are (a) boxes whose content is wider than the box — a scroller
 * that scrolls sideways or a clip that hides a control — and (b) elements that
 * escape the viewport with no clipping ancestor. Intentional sideways
 * scrollers (the terminal, a tab strip) carry `data-allow-overflow-x`;
 * `text-overflow: ellipsis` truncation and 1 px screen-reader boxes are fine.
 */
export async function overflow(page) {
  return page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    const clippedByAncestor = (el) => {
      for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
        if (getComputedStyle(a).overflowX !== "visible") return true;
      }
      return false;
    };
    const offenders = [];
    for (const el of document.body.querySelectorAll("*")) {
      if (offenders.length >= 10) break;
      if (el.closest("[data-allow-overflow-x]")) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 1 || r.height <= 1) continue;
      const style = getComputedStyle(el);
      if (style.visibility === "hidden" || style.display === "none") continue;
      const describe = (kind) => ({
        kind, tag: el.tagName, cls: String(el.className).slice(0, 80),
        text: (el.textContent ?? "").trim().slice(0, 40),
        left: Math.round(r.left), right: Math.round(r.right),
        scrollWidth: el.scrollWidth, clientWidth: el.clientWidth,
      });
      if (style.overflowX !== "visible" && style.textOverflow !== "ellipsis" && el.scrollWidth > el.clientWidth + 1) {
        offenders.push(describe("content wider than its box"));
      } else if ((r.right > width + 1 || r.left < -1) && !clippedByAncestor(el)) {
        offenders.push(describe("escapes the viewport"));
      }
    }
    return { scroll: document.documentElement.scrollWidth - width, offenders };
  });
}

/** Size of every visible element matching `selector` (CSS, or Puppeteer's "xpath/…" and "::-p-text(…)"). */
export async function targets(page, selector) {
  const found = [];
  for (const handle of await page.$$(selector)) {
    const box = await handle.evaluate((el) => {
      const r = el.getBoundingClientRect();
      return { label: el.getAttribute("aria-label") || (el.textContent ?? "").trim().slice(0, 30), width: Math.round(r.width), height: Math.round(r.height) };
    });
    if (box.width > 0 && box.height > 0) found.push(box);
  }
  return found;
}

/** The assertions every check makes: no page overflow; on phones, 44 px primary controls that exist. */
export async function expectLayout(t, { primary = [] } = {}) {
  const { scroll, offenders } = await overflow(t.page);
  assert.ok(scroll <= 1, `page scrolls ${scroll}px sideways at ${t.profile}`);
  assert.deepEqual(offenders, [], `elements escape the viewport at ${t.profile}`);
  if (!t.isPhone) return;
  for (const selector of primary) {
    const found = await targets(t.page, selector);
    assert.ok(found.length > 0, `no visible primary control matches ${selector} at ${t.profile}`);
    for (const target of found) {
      assert.ok(target.width >= 44 && target.height >= 44, `${selector} "${target.label}" is ${target.width}×${target.height} at ${t.profile}; primary controls are ≥44 px`);
    }
  }
}

/** Every WebSocket the page opens and every frame it sends, from the DevTools protocol. */
export async function watchSockets(page) {
  const cdp = await page.createCDPSession();
  await cdp.send("Network.enable");
  const created = [];
  const sent = [];
  const urls = new Map();
  cdp.on("Network.webSocketCreated", ({ requestId, url }) => { urls.set(requestId, url); created.push(url); });
  cdp.on("Network.webSocketFrameSent", ({ requestId, response }) => sent.push({ url: urls.get(requestId), bytes: response.payloadData.length }));
  return { created, sent, terminal: () => created.filter((u) => new URL(u).pathname.startsWith("/ws/terminal")) };
}

/** Simulate a notch/home indicator: the app reads these variables, which default to env(). */
export async function setSafeArea(page, { top = 0, right = 0, bottom = 0, left = 0 }) {
  await page.evaluate((insets) => {
    for (const [side, px] of Object.entries(insets)) document.documentElement.style.setProperty(`--safe-${side}`, `${px}px`);
  }, { top, right, bottom, left });
}

export async function rect(page, selector) {
  return page.$eval(selector, (el) => { const r = el.getBoundingClientRect(); return { top: r.top, left: r.left, bottom: r.bottom, right: r.right, width: r.width, height: r.height }; });
}

export async function waitForText(page, text, timeout = 10_000) {
  await page.waitForFunction((needle) => document.body.innerText.includes(needle), { timeout }, text);
}
