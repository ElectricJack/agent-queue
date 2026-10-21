import { useParams } from "react-router-dom";
import { AskDetail } from "../../components/asks/AskDetail";
import { useFocusChrome } from "./focusChrome";
export default function FocusAsk() {
  const { askId = "" } = useParams();
  useFocusChrome({ title: "Request" });
  return askId ? <AskDetail key={askId} askId={askId} /> : <p role="alert">Request id is missing.</p>;
}
