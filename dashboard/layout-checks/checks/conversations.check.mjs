// A cold conversation link remains readable and replyable in the normal shell.
import assert from "node:assert/strict";
import { expectLayout, waitForText } from "../probes.mjs";
export const name = "conversations";
export const profiles = ["phone-390", "desktop"];
export async function run(t) {
  const conversation = { id: "conv-phone", thread_id: "conversation:conv-phone", created_by: "human:discord:111",
    transport: "discord", state: "open", inputs: [{ received_at: 100 }] };
  t.stub.override("POST /api/supervisor_inbox/status", () => ({ success: true, enabled: true }));
  t.stub.override("POST /api/supervisor_inbox/history", body => ({ success: true,
    conversations: body.conversation_id === "missing" ? [] : [conversation], next_before: null, next_before_id: null }));
  t.stub.override("GET /api/sessions/supervisor-global/messages", () => ({ success: true, count: 1, messages: [{
    id: "phone-message", from_id: "supervisor-global", from_kind: "session", to_kind: "user", created_at: 100,
    body: "Long conversation receipt: " + "unbroken-identifier".repeat(30),
  }] }));
  t.stub.override("POST /api/sessions/supervisor-global/message", () => ({ success: true, message_id: "phone-reply", state: "queued" }));
  await t.page.goto(t.url("/conversations?conversation=conv-phone"), { waitUntil: "networkidle0" });
  await waitForText(t.page, "Long conversation receipt");
  await expectLayout(t, { primary: ['select[aria-label="Conversation"]', 'button[aria-label="Send"]'] });
  await t.shot("history");
  await t.page.type('textarea[aria-label="Message the supervisor"]', "Reply from the phone");
  await t.page.click('button[aria-label="Send"]');
  await t.page.waitForFunction(() => [...document.querySelectorAll("div")].some(el => el.textContent === "Reply from the phone"));
  const sent = t.stub.requests.find(row => row.method === "POST" && row.path === "/api/sessions/supervisor-global/message");
  assert.equal(JSON.parse(sent.body).thread_id, "conversation:conv-phone");
  assert.equal(JSON.parse(sent.body).body, "Reply from the phone");
  await expectLayout(t);
  await t.page.goto(t.url("/conversations?conversation=missing"), { waitUntil: "networkidle0" });
  await waitForText(t.page, "No conversation with that id");
  assert.equal(await t.page.$('textarea[aria-label="Message the supervisor"]'), null);
  await expectLayout(t);
}
