import { useParams } from "react-router-dom";
import { AskAlias } from "../../components/asks/AskAlias";

export default function ReviewPage() {
  const { reviewId = "" } = useParams();
  return <AskAlias kind="review" id={reviewId} />;
}
