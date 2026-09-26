export function senderLabel(msg: {
  from_kind: string;
  from_id: string;
  body_kind?: string | null;
}): string {
  if (msg.from_kind === "user") {
    if (msg.from_id === "dashboard") return "You";
    if (/^discord:\d+$/.test(msg.from_id)) {
      return `Discord · ${msg.from_id.slice("discord:".length)} (verified)`;
    }
  }
  if (msg.from_kind === "session" && msg.from_id === "supervisor-global") return "Agent Q";
  return msg.from_id;
}
