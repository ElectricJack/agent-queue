import assert from "node:assert/strict";
import { REVIEW_ID, REVIEW_MARKDOWN } from "../fixtures/reviews.mjs";

export const name = "human-asks";
export const profiles = ["phone-390", "desktop"];

export async function run(t) {
  const version = { decision_generation: 2, source_fingerprint: "source-hash", feedback_version: 3 };
  const ask = { ...version, id: "ask-rollout", project_id: "fixture", source_kind: "review", source_key: REVIEW_ID,
    display_version: 1, severity: "high", next_actor: "human", context_state: "complete",
    application_state: "not_started", decision_state: "pending", context: {
      title: "Rollout decision", why: "Verify feedback before release", waiting_effect: "Implementation stays held",
      requested_by: { label: "Fixture supervisor", origin: "review" },
    } };
  const detail = { success: true, ask, expected: version,
    actions: [{ action: "discuss", enabled: true }, { action: "decide", option_key: "approve", label: "Approve", enabled: true }],
    inputs: [], history: [], obligations: [], operations: [], source_health: "healthy", document: { revision: 2 } };
  const prepared = { success: true, preview: { id: "preview-rollout", packet_sha256: "packet-hash",
    expires_at: Date.now() / 1000 + 300, packet: {
      intent: { option_key: "approve" }, context: { choices: [{ key: "approve", effect: "Approve with follow-up" }] },
      domain: { review_revision: 2, content_sha256: "content-hash", effective_option: "approve", feedback: [
        { feedback_key: "comment:restart", original_text: "Cover restart", original_revision: 2,
          original_author: "human:operator", disposition: "follow_up", acceptance_text: "Verify restart receipts", required_before_release: true },
      ] },
    } } };
  t.stub.override("POST /api/review/ask-show", () => detail);
  t.stub.override("POST /api/review/ask-list", () => ({ success: true, items: [ask], count: 1, snapshot_at: 1, source_health: "healthy", identity_hash: "one" }));
  t.stub.override("POST /api/review/show", () => ({ success: true,
    review: { id: REVIEW_ID, project_id: "fixture", title: "Rollout decision", kind: "spec", state: "in_review", current_revision: 2, decider: "user" },
    revision: { revision: 2, content: REVIEW_MARKDOWN[2] }, revisions: [{ revision: 2 }], comments: [], attachments: [], vault_state: "ok",
    response_route: { kind: "new_task", summary: "Follow-up precedes release", class_summaries: {} },
  }));
  t.stub.override("POST /api/review/ask-prepare-decision", () => prepared);
  t.stub.override("POST /api/review/ask-decide", () => ({ success: true, decision_id: "decision-rollout", application_state: "pending" }));
  let acknowledge;
  t.stub.override("POST /api/review/ask-reply", () => new Promise(resolve => { acknowledge = () => resolve({ success: true, input_id: "input-rollout" }); }));
  await t.page.goto(t.url(`/focus/asks/${ask.id}`), { waitUntil: "networkidle0" });
  const discuss = 'section[aria-label="Discuss"] textarea';
  const approve = 'button::-p-text(Prepare Approve)';
  await t.page.waitForSelector(discuss);
  await t.page.focus(discuss);
  await t.page.keyboard.type("How is restart handled?");
  assert.equal(await t.page.$eval(approve, el => el.disabled), true, "discussion draft must hold approval");
  await t.page.keyboard.press("Tab");
  assert.equal(await t.page.evaluate(() => document.activeElement.textContent.trim()), "Save discussion");
  await t.page.keyboard.press("Enter");
  await t.page.waitForFunction(() => document.body.textContent.includes("Saving discussion"));
  assert.equal(await t.page.$eval(approve, el => el.disabled), true, "save acknowledgement must precede approval");
  assert.ok(acknowledge, "save request was sent");
  acknowledge();
  await t.page.waitForFunction(() => [...document.querySelectorAll("button")].some(el => el.textContent === "Prepare Approve" && !el.disabled));
  await (await t.page.$(approve)).focus();
  await t.page.keyboard.press("Enter");
  await t.page.waitForSelector('section[aria-label="Decision preview"]');
  const previewText = await t.page.$eval('section[aria-label="Decision preview"]', el => el.textContent);
  for (const text of ["Exact revision 2", "Cover restart", "Verify restart receipts", "required before release", "packet-hash"]) assert.ok(previewText.includes(text), text);
  await t.shot("prepared-feedback");
  version.feedback_version++;
  t.stub.pushEvent({ seq: 999, event_type: "review.comment_added", data: { review_id: REVIEW_ID }, timestamp: Date.now() / 1000 });
  await t.page.waitForFunction(() => document.body.textContent.includes("Preview is stale"));
  const confirm = 'button::-p-text(Confirm prepared decision)';
  assert.equal(await t.page.$eval(confirm, el => el.disabled), true);
  await (await t.page.$(approve)).focus();
  await t.page.keyboard.press("Enter");
  await t.page.waitForFunction(() => [...document.querySelectorAll("button")].some(el => el.textContent === "Confirm prepared decision" && !el.disabled));
  await (await t.page.$(confirm)).focus();
  await t.page.keyboard.press("Enter");
  await t.page.waitForFunction(() => !document.querySelector('section[aria-label="Decision preview"]'));
  assert.equal(t.stub.requests.filter(r => r.path === "/api/review/ask-decide").length, 1);
  assert.equal(t.stub.requests.filter(r => r.path === "/api/review/ask-reply").length, 1);
  assert.ok(await t.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "narrow viewport overflow");
}
