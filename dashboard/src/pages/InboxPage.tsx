import { AskInbox } from "../components/asks/AskInbox";

export default function InboxPage() {
  return <div className="dashboard-page h-full overflow-y-auto p-3 sm:p-6">
    <h1 className="text-xl font-semibold">Needs you</h1>
    <AskInbox />
  </div>;
}
