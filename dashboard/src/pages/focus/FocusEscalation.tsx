import { useParams } from "react-router-dom";
import { AskAlias } from "../../components/asks/AskAlias";
import { useFocusChrome } from "./focusChrome";

export default function FocusEscalation() {
  const { escalationId = "" } = useParams();
  useFocusChrome({ title: "Request" });
  return <AskAlias kind="escalation" id={escalationId} />;
}
