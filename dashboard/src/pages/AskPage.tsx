import { useParams } from "react-router-dom";
import { AskDetail } from "../components/asks/AskDetail";

export default function AskPage() {
  const { askId = "" } = useParams();
  return <div className="h-full overflow-y-auto [overflow-wrap:anywhere]"><AskDetail key={askId} askId={askId} /></div>;
}
