import { useParams } from "react-router-dom";
import { AskAlias } from "../components/asks/AskAlias";

export default function EscalationPage() {
  const { escalationId = "" } = useParams();
  return <AskAlias kind="escalation" id={escalationId} />;
}
