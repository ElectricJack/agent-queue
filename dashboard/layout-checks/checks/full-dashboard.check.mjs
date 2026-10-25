// Real geometry and usable controls on the canonical pages, including old links.
import assert from "node:assert/strict";
import { expectLayout, waitForText } from "../probes.mjs";
import { CHILD_TASK_ID, TASK_IDS, SESSION } from "../fixtures/base.mjs";
import { STARTED, SESSIONS, showSession, ENDED_SESSION } from "../fixtures/session.mjs";
import { REPORT } from "../fixtures/report.mjs";

export const name = "full-dashboard";
export const profiles = ["phone-390", "desktop"];

export async function run(t) {
  const menu = t.isPhone ? ['[aria-label="Open navigation"]'] : [];
  await t.page.goto(t.url(`/focus/tasks/${TASK_IDS[0]}?tab=details#kept`), { waitUntil: "networkidle0" });
  assert.equal(new URL(t.page.url()).pathname, `/tasks/${TASK_IDS[0]}`);
  assert.equal(new URL(t.page.url()).hash, "#kept");
  await waitForText(t.page, "Ünïcödé");
  await expectLayout(t, { primary: [...menu, "main button"] });
  await t.shot("task");
  await t.page.click(`a[href="/tasks/${CHILD_TASK_ID}"]`);
  await waitForText(t.page, "Dotted child task");
  await expectLayout(t);
  await t.page.goBack();
  await waitForText(t.page, "Ünïcödé");

  if (t.isPhone) {
    assert.equal(await t.page.$("aside nav"), null);
    await t.page.click('[aria-label="Open navigation"]');
    await t.page.waitForSelector('[aria-label="Navigation"]');
    assert.equal(await t.page.$('a[href="/focus"]'), null);
    for (const path of ["/inbox", "/conversations", "/reviews"]) {
      assert.ok(await t.page.$(`a[href="${path}"]`), `missing menu destination ${path}`);
    }
    assert.ok(await t.page.$('[aria-label="Agent flock"]'), "missing agent navigation");
    await t.page.click('a[href="/inbox"]');
  } else await t.page.goto(t.url("/inbox"), { waitUntil: "networkidle0" });
  await waitForText(t.page, "Requests");
  await expectLayout(t, { primary: [...menu, 'nav[aria-label="Request views"] button'] });

  await t.page.goto(t.url(`/sessions/${SESSION}?started=${STARTED}`), { waitUntil: "networkidle0" });
  await t.page.waitForSelector("[data-interactive-terminal]");
  await expectLayout(t, { primary: [...menu, 'main button'] });
  await t.shot("session");
  t.stub.override("POST /api/system/session-show", body => body.session_id === SESSION
    ? { session: { ...SESSIONS[SESSION], started_at: STARTED + 50 } } : showSession(body));
  await t.page.reload({ waitUntil: "networkidle0" });
  await waitForText(t.page, "This session restarted");
  assert.equal(await t.page.$("[data-interactive-terminal]"), null);
  await t.page.click('button::-p-text(Watch the new process)');
  await t.page.waitForSelector("[data-interactive-terminal]");
  assert.ok(t.page.url().includes(`started=${STARTED + 50}`));
  await t.page.goto(t.url(`/sessions/${ENDED_SESSION}`), { waitUntil: "networkidle0" });
  await waitForText(t.page, "Task closed: pass");
  assert.ok(await t.page.$(`a[href="/sessions/${SESSION}"]`));
  await expectLayout(t);

  await t.page.goto(t.url(`/reports/${REPORT}`), { waitUntil: "networkidle0" });
  await waitForText(t.page, "Morning report");
  assert.ok(await t.page.$(`a[href="/tasks/${CHILD_TASK_ID}"]`));
  await expectLayout(t);

  await t.page.goto(t.url("/focus/conversations/phone-thread"), { waitUntil: "networkidle0" });
  assert.equal(new URL(t.page.url()).searchParams.get("conversation"), "phone-thread");
  await t.page.waitForSelector('[aria-label="Message the supervisor"]');
  await waitForText(t.page, "Phone conversation");
  await expectLayout(t, { primary: [...menu, '[aria-label="Conversation"]', '[aria-label="Send"]'] });
  await t.page.type('[aria-label="Message the supervisor"]', "Reply from the full dashboard");
  await t.page.click('[aria-label="Send"]');
  await t.page.waitForFunction(() => document.querySelector('[aria-label="Message the supervisor"]').value === "");
  const reply = t.stub.requests.find(r => r.path === "/api/sessions/supervisor-global/message");
  assert.equal(JSON.parse(reply.body).thread_id, "conversation:phone-thread");
  await t.shot("conversation");
}
