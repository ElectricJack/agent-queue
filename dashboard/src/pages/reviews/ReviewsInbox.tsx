import { AskInbox } from "../focus/FocusInbox";
import PullRequestsSection from "./PullRequestsSection";

/** Pull-request workspaces stay distinct from document decision counts. */
export default function ReviewsInbox() {
  return <div className="h-full space-y-4 overflow-y-auto p-3">
    <h1 className="text-xl font-semibold">Reviews</h1>
    <PullRequestsSection />
    <h2 className="text-lg font-semibold">Document reviews</h2>
    <AskInbox sourceKind="review" />
  </div>;
}
