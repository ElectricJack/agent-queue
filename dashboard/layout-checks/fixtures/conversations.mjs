export const routes = {
  "POST /api/supervisor_inbox/status": () => ({ success: true, enabled: true }),
  "POST /api/supervisor_inbox/history": () => ({ success: true, conversations: [{
    id: "phone-thread", thread_id: "conversation:phone-thread", created_by: "human:discord:operator",
    state: "open", inputs: [{ received_at: 1790000000 }],
  }] }),
  "GET /api/sessions/supervisor-global/messages": () => ({ messages: [{
    id: "phone-message", body: "Phone conversation " + "unbroken".repeat(40), created_at: 1790000000,
    from_kind: "session", from_id: "supervisor-global", to_kind: "user", to_id: "dashboard",
  }] }),
  "POST /api/sessions/supervisor-global/message": () => ({ success: true, message_id: "reply-phone" }),
};
