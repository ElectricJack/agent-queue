import { useParams } from "react-router-dom";
import { AskDetail } from "../components/asks/AskDetail";

export default function AskPage() {
  const { askId = "" } = useParams();
  return <AskDetail key={askId} askId={askId} />;
}
