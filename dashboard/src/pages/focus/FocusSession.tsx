import { useParams } from "react-router-dom";
import { FocusUnavailable } from "./FocusNotice";

/** Replaced by Task 4 (the watch-only session). */
export default function FocusSession() {
  const { sessionId = "" } = useParams();
  return <FocusUnavailable title="Session" fullHref={`/sessions/${encodeURIComponent(sessionId)}`} />;
}
