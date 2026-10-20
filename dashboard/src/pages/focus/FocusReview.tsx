import { useParams } from "react-router-dom";
import { AskAlias } from "../../components/asks/AskAlias";
import { useFocusChrome } from "./focusChrome";

export default function FocusReview() {
  const { reviewId = "" } = useParams();
  useFocusChrome({ title: "Request" });
  return <AskAlias kind="review" id={reviewId} />;
}
