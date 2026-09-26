import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { reportGet, type ReportItem } from "../../api/client";

function EvidenceItems({ items }: { items: ReportItem[] }) {
  if (!items.length) return <p className="text-sm text-gray-400">None recorded.</p>;
  return <ul className="space-y-3">{items.map((item, index) => <li key={`${item.refs.join(":")}-${index}`}>
    <p className="whitespace-pre-wrap">{item.text} {item.late && <span className="text-amber-300">(late arrival)</span>}</p>
    {item.shipment === "unknown" && <p className="text-amber-300">Shipment unknown.</p>}
    {item.task_id && <Link className="text-blue-300 underline" to={`/tasks/${encodeURIComponent(item.task_id)}`}>Task {item.task_id}</Link>}
    {item.prior_verification && <p className="whitespace-pre-wrap text-sm text-gray-400">Agent-reported verification: {item.prior_verification}</p>}
    <p className="break-all text-xs text-gray-500">Evidence: {item.refs.join(", ")}</p>
  </li>)}</ul>;
}

export default function MorningReportPage() {
  const { reportId } = useParams();
  const query = useQuery({
    queryKey: ["report", reportId],
    enabled: !!reportId,
    queryFn: async () => {
      const { data } = await reportGet({ body: { report_id: reportId! } });
      if (!data) throw new Error("Report could not be read.");
      return data.report;
    },
    refetchInterval: (q) => q.state.data && ["building", "ready", "authoring"].includes(q.state.data.state) ? 30_000 : false,
  });
  if (!reportId) return <div role="alert" className="p-6">Report id is missing.</div>;
  if (query.isPending) return <div role="status" className="p-6">Loading report…</div>;
  if (query.isError) return <div role="alert" className="p-6">Could not read report: {query.error.message}</div>;
  const row = query.data;
  const content = row.report;
  const coverage = content?.coverage;
  const gaps = Array.isArray(coverage?.gaps) ? coverage.gaps as { source: string; reason: string }[] : [];
  const warnings = Array.isArray(coverage?.warnings) ? coverage.warnings as string[] : [];
  const window = coverage?.window as { omitted_interval?: { since: number; until: number } | null } | undefined;
  const date = (seconds: number) => new Date(seconds * 1000).toISOString();
  return <article className="mx-auto max-w-4xl space-y-6 p-6 text-gray-100">
    <header>
      <h1 className="text-2xl font-semibold">Morning report · {row.local_date}</h1>
      <p className="text-sm text-gray-400">{row.timezone} · {row.state}{row.reason ? ` · ${row.reason}` : ""}</p>
      <p className="text-sm text-gray-400">Coverage: {date(row.window_start)} to {date(row.window_end)} (end excluded)</p>
      {row.is_fallback && content && <p className="text-sm text-gray-400">Deterministic fallback from recorded evidence.</p>}
      {["ready", "authoring"].includes(row.state) && <p className="text-sm text-gray-400">Awaiting author until {date(row.author_deadline)}.</p>}
    </header>
    {!content && <p>{row.state === "skipped" ? "This report was skipped. Coverage did not advance." : "Report content is not available yet."}</p>}
    {content && <>
      <p className="whitespace-pre-wrap">{content.summary}</p>
      <section aria-label="Coverage details" className="rounded border border-gray-700 p-4">
        <h2 className="font-semibold">{coverage?.complete ? "Sources read successfully" : "Partial coverage"}</h2>
        {gaps.map((gap, i) => <p className="text-amber-300" key={i}>{gap.source}: {gap.reason}</p>)}
        {window?.omitted_interval && <p className="text-amber-300">Lookback capped. Omitted {date(window.omitted_interval.since)} to {date(window.omitted_interval.until)}.</p>}
        {warnings.map((warning) => <p className="text-sm text-gray-400" key={warning}>{warning.replace(/_/g, " ")}</p>)}
      </section>
      {content.projects.map((project) => <section className="space-y-4" key={project.id}>
        <h2 className="text-xl font-semibold">{project.name || project.id}</h2>
        <h3 className="font-semibold">Landed changes</h3><EvidenceItems items={project.landed} />
        <h3 className="font-semibold">Pending or unknown shipment</h3><EvidenceItems items={project.pending} />
        <h3 className="font-semibold">Failures and unresolved risks</h3><EvidenceItems items={project.failures} />
        <h3 className="font-semibold">Manual checks</h3>
        {!project.manual_checks.length && <p className="text-sm text-gray-400">No evidence-grounded manual checks were supplied.</p>}
        <ul className="space-y-3">{project.manual_checks.map((check, i) => <li key={i}>
          <p>{check.action} · {check.surface}</p><p>Expected: {check.expected_result}</p><p>{check.reason}</p>
          <p className="text-sm text-gray-400">Prior verification (agent-reported): {check.prior_verification || "None"} · Confidence: {check.confidence}</p>
          <p className="text-xs text-gray-500">Evidence: {check.refs.join(", ")}</p>
        </li>)}</ul>
      </section>)}
      {!!content.global_facts?.length && <section><h2 className="text-xl font-semibold">Fleet activity</h2><EvidenceItems items={content.global_facts ?? []} /></section>}
      {!!content.omitted?.items && <p className="text-amber-300">{content.omitted?.items} items omitted from this bounded report.</p>}
    </>}
  </article>;
}
