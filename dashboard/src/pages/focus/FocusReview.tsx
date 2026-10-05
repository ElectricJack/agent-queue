import { useParams } from "react-router-dom";

import { ReviewPane } from "../../panes/review";
import { useFocusChrome } from "./focusChrome";

/**
 * `/focus/reviews/:reviewId` — the shared reader (spec §4.1: reuse the page
 * component, one fetch policy) under the focus shell, so the link in a
 * review-ready post opens on a phone and its decision bar works there.
 */
export default function FocusReview() {
  const { reviewId = "" } = useParams();
  useFocusChrome({ title: "Review", fullHref: `/reviews/${encodeURIComponent(reviewId)}` });
  if (!reviewId) return <p role="alert" className="p-4 text-sm text-red-300">Review id is missing.</p>;
  return <ReviewPane reviewId={reviewId} />;
}