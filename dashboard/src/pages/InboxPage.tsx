import { AskInbox } from "../components/asks/AskInbox";

export default function InboxPage() {
  return <div className="h-full overflow-y-auto [overflow-wrap:anywhere]">
    <h1 className="px-3 pt-3 text-xl font-semibold">Requests</h1><AskInbox />
  </div>;
}
