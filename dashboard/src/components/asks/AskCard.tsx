import { Link } from "react-router-dom";
import type { Ask } from "../../api/asks";
import { focusAskHref } from "../../pages/focus/routes";

export function AskCard({ ask }: { ask: Ask }) {
  return <article className="rounded-lg border border-gray-800 p-3">
    <Link className="block min-h-11 py-2 font-medium text-indigo-200 hover:underline" to={focusAskHref(ask.id)}>
      {ask.context.title || ask.id}
    </Link>
    <p className="text-sm text-gray-300">{ask.context.why}</p>
    <p className="mt-2 text-xs text-gray-400">{ask.project_id} · {ask.severity} · generation {ask.decision_generation}</p>
    <p className="text-xs text-gray-400">{ask.application_state === "not_started" ? ask.decision_state : ask.application_state}</p>
    {ask.answer_pending && <p role="status" className="text-sm text-indigo-200">Supervisor answer pending · decision still needs you</p>}
    {ask.held_while_paused && <p className="text-sm text-amber-200">Held while paused</p>}
  </article>;
}
