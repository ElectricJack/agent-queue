import { useParams } from "react-router-dom";
import { MorningReportContent } from "../reports/MorningReportPage";
import { useFocusChrome } from "./focusChrome";
import { focusTaskHref } from "./routes";

/** `/focus/reports/:reportId` — the read page's content; task evidence stays in focus. */
export default function FocusReport() {
  const { reportId = "" } = useParams();
  useFocusChrome({ title: "Morning report", fullHref: `/reports/${encodeURIComponent(reportId)}` });
  return <MorningReportContent reportId={reportId} taskHref={focusTaskHref} />;
}
