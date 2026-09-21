import { useParams } from "react-router-dom";

import { ReviewPane } from "../../panes/review";

/** The deep-link surface for a document review, including Discord links. */
export default function ReviewPage() {
  const { reviewId } = useParams();
  if (!reviewId) return <div role="alert" className="p-5 text-sm text-red-300">Review id is missing.</div>;
  return <ReviewPane reviewId={reviewId} />;
}
