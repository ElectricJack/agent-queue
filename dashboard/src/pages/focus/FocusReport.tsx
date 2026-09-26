import { useParams } from "react-router-dom";
import { FocusUnavailable } from "./FocusNotice";

/** Replaced by Task 6 (the focus report). */
export default function FocusReport() {
  const { reportId = "" } = useParams();
  return <FocusUnavailable title="Report" fullHref={`/reports/${encodeURIComponent(reportId)}`} />;
}
