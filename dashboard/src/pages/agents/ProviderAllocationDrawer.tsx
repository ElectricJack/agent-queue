import { useEffect, useId, useState, type ReactNode } from "react";
import { ExclamationTriangleIcon, XMarkIcon } from "@heroicons/react/24/outline";
import type {
  ProviderAllocationApplyResponse,
  ProviderAllocationGroup,
  ProviderAllocationPreviewResponse,
  ProviderAllocationProject,
} from "../../api/client";
import { useProviderAllocationApply, useProviderAllocationPreview } from "../../api/providerAllocation";
import { providerErrorText } from "../../api/providers";
import AllocationConfirm from "./AllocationConfirm";
import AllocationResult from "./AllocationResult";
import {
  applyOutcome,
  draftProblem,
  initialDraft,
  previewBody,
  profileBounds,
  providerLabel,
  type AllocationDraft,
  type DrainMode,
  type Participation,
  type PreferenceMode,
} from "./allocation";

type Step = "edit" | "confirm" | "result";

const inputClass = "mt-1 w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none";

/**
 * The provider allocation drawer: edit a request, preview it, confirm the
 * preview, apply its token (spec §Dashboard).
 *
 * The request never reaches apply: apply sends the reviewed preview's token,
 * so what the operator confirmed is what the daemon applies, and a fleet that
 * moved in between is refused (``preview_stale``) with the fresh preview put
 * in front of the operator again.  Graceful drain is the default; interrupting
 * busy work is a separate danger choice here and a typed confirmation of the
 * exact busy set on the next page.
 */
export default function ProviderAllocationDrawer({ group, projects, onClose }: {
  group: ProviderAllocationGroup;
  projects: ProviderAllocationProject[];
  onClose: () => void;
}) {
  const label = providerLabel(group.provider, group.vendor);
  const [draft, setDraft] = useState<AllocationDraft>(() => initialDraft(group));
  const [step, setStep] = useState<Step>("edit");
  const [plan, setPlan] = useState<ProviderAllocationPreviewResponse | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editError, setEditError] = useState<string | null>(null);
  const [result, setResult] = useState<ProviderAllocationApplyResponse | null>(null);
  const preview = useProviderAllocationPreview();
  const apply = useProviderAllocationApply();
  const busy = preview.isPending || apply.isPending;

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const runPreview = () => {
    setEditError(null);
    preview.mutate(previewBody(draft, group), {
      onSuccess: (data) => { setPlan(data); setNotice(null); setStep("confirm"); },
      onError: (error) => setEditError(providerErrorText(error)),
    });
  };

  const onApplied = (answer: ProviderAllocationApplyResponse) => {
    if (applyOutcome(answer) !== "refused") {
      setResult(answer);
      setStep("result");
      return;
    }
    const code = answer.error_code ?? "";
    const text = answer.error || "The daemon refused this allocation.";
    if (code === "preview_stale" && answer.preview) {
      setPlan(answer.preview);
      setNotice("The fleet changed since this preview. Review the fresh preview below and apply again.");
      return;
    }
    if (code === "preview_stale" || code === "preview_unknown") {
      // Nothing left to confirm: the request itself no longer applies, or the
      // daemon forgot the token (it restarted, or the preview expired).
      setPlan(null);
      setStep("edit");
      setEditError(text + " Preview again.");
      return;
    }
    if (answer.preview) setPlan(answer.preview);
    setNotice(text);
  };

  const runApply = (body: { authorize_busy_interrupt?: string[]; allow_pinned_wait?: boolean }) => {
    if (!plan) return;
    setNotice(null);
    apply.mutate({ preview_token: plan.preview_token, ...body }, {
      onSuccess: onApplied,
      onError: (error) => setNotice(providerErrorText(error)),
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/60" onClick={busy ? undefined : onClose} aria-hidden />
      <aside role="dialog" aria-modal="true" aria-label={label + " allocation"}
        className="flex h-full w-full max-w-xl flex-col border-l border-gray-700 bg-gray-900 shadow-2xl">
        <header className="flex items-start justify-between gap-4 border-b border-gray-700 px-5 py-4">
          <div>
            <p className="text-xs uppercase tracking-wider text-gray-500">
              {step === "edit" ? "Provider allocation · 1 of 2" : step === "confirm" ? "Provider allocation · 2 of 2" : "Provider allocation"}
            </p>
            <h2 className="text-lg font-semibold text-gray-100">{label}</h2>
          </div>
          <button type="button" aria-label="Close allocation" onClick={onClose} disabled={busy}
            className="rounded p-1 text-gray-400 hover:bg-gray-800 hover:text-gray-200 disabled:opacity-40">
            <XMarkIcon className="h-4 w-4" />
          </button>
        </header>
        <div className="flex-1 overflow-y-auto px-5 py-4 text-sm">
          {step === "edit" && (
            <AllocationEditor group={group} projects={projects} draft={draft} onChange={setDraft}
              error={editError} pending={preview.isPending} onPreview={runPreview} />
          )}
          {step === "confirm" && plan && (
            <AllocationConfirm key={plan.preview_token} preview={plan} notice={notice} pending={apply.isPending}
              onBack={() => { setStep("edit"); setNotice(null); }} onApply={runApply} />
          )}
          {step === "result" && result && <AllocationResult result={result} onDone={onClose} />}
        </div>
      </aside>
    </div>
  );
}

function Fieldset({ legend, hint, children, danger = false }: {
  legend: string; hint?: string; children: ReactNode; danger?: boolean;
}) {
  return (
    <fieldset className={"space-y-1.5 rounded-md border p-3 " + (danger ? "border-red-900/70 bg-red-950/20" : "border-gray-800")}>
      <legend className={"px-1 text-xs font-medium uppercase tracking-wide " + (danger ? "text-red-300" : "text-gray-400")}>{legend}</legend>
      {hint && <p className="text-xs text-gray-500">{hint}</p>}
      {children}
    </fieldset>
  );
}

function Radio({ name, checked, onChange, children }: {
  name: string; checked: boolean; onChange: () => void; children: ReactNode;
}) {
  return (
    <label className="flex items-start gap-2 text-sm text-gray-300">
      <input type="radio" name={name} checked={checked} onChange={onChange} className="mt-1 accent-indigo-500" />
      <span>{children}</span>
    </label>
  );
}

const PARTICIPATION: { value: Participation; label: string }[] = [
  { value: "", label: "Leave unchanged" },
  { value: "pool", label: "Pool — workers pull queued work" },
  { value: "task", label: "Task — stop pooling; each task gets its own session" },
];

const PREFERENCE: { value: PreferenceMode; label: string }[] = [
  { value: "", label: "Leave unchanged" },
  { value: "prefer", label: "Prefer this provider for unpinned work" },
  { value: "clear", label: "Clear the preference" },
];

/** Step 1: the request, in the spec's terms; nothing changes until apply. */
function AllocationEditor({ group, projects, draft, onChange, error, pending, onPreview }: {
  group: ProviderAllocationGroup;
  projects: ProviderAllocationProject[];
  draft: AllocationDraft;
  onChange: (draft: AllocationDraft) => void;
  error: string | null;
  pending: boolean;
  onPreview: () => void;
}) {
  const id = useId();
  const [dangerOpen, setDangerOpen] = useState(draft.drain === "interrupt-busy");
  const problem = draftProblem(draft);
  const set = (patch: Partial<AllocationDraft>) => onChange({ ...draft, ...patch });
  const setDrain = (drain: DrainMode) => set({ drain });
  const toggleProfile = (profileId: string, checked: boolean) => set({
    profileIds: checked ? [...draft.profileIds, profileId] : draft.profileIds.filter((item) => item !== profileId),
  });

  return (
    <div className="space-y-4">
      <Fieldset legend="Profiles" hint="Ordinary worker profiles on this provider. Profiles are global: a change applies to every project.">
        {(group.profiles ?? []).map((profile) => (
          <div key={profile.profile_id} className="flex items-center justify-between gap-2">
            <label className="flex items-center gap-2 text-sm text-gray-200">
              <input type="checkbox" checked={draft.profileIds.includes(profile.profile_id)}
                onChange={(event) => toggleProfile(profile.profile_id, event.target.checked)} className="accent-indigo-500" />
              {profile.profile_id}
            </label>
            <span className="shrink-0 font-mono text-[10px] text-gray-500">
              {profile.intelligence_class} · {profile.lifecycle}{profile.lifecycle === "pool" ? " " + profileBounds(profile) : ""}
            </span>
          </div>
        ))}
      </Fieldset>

      <Fieldset legend="Lifecycle">
        {PARTICIPATION.map((option) => (
          <Radio key={option.value || "unchanged"} name={id + "-participation"} checked={draft.participation === option.value}
            onChange={() => set({ participation: option.value })}>{option.label}</Radio>
        ))}
      </Fieldset>

      <Fieldset legend="Bounds" hint="Per selected pool profile, exactly as `aq pool scale` sets them. The preview reports the provider-wide total.">
        <label className="flex items-center gap-2 text-sm text-gray-300">
          <input type="checkbox" checked={draft.changeBounds} onChange={(event) => set({ changeBounds: event.target.checked })}
            className="accent-indigo-500" />
          Change per-profile bounds
        </label>
        {draft.changeBounds && (
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-xs text-gray-400" htmlFor={id + "-min"}>
              Minimum per profile
              <input id={id + "-min"} type="number" min={0} inputMode="numeric" value={draft.bounds.min}
                onChange={(event) => set({ bounds: { ...draft.bounds, min: event.target.value } })} className={inputClass} />
            </label>
            <label className="text-xs text-gray-400" htmlFor={id + "-max"}>
              Maximum per profile
              <input id={id + "-max"} type="number" min={1} inputMode="numeric" value={draft.bounds.max} placeholder="Unbounded"
                onChange={(event) => set({ bounds: { ...draft.bounds, max: event.target.value } })} className={inputClass} />
            </label>
          </div>
        )}
      </Fieldset>

      <Fieldset legend="New unpinned work" hint="A project's preferred provider for tasks without an explicit profile. Pinned tasks ignore it.">
        <label className="block text-xs text-gray-400" htmlFor={id + "-project"}>
          Project
          <select id={id + "-project"} value={draft.preferenceProject} className={inputClass}
            onChange={(event) => set({ preferenceProject: event.target.value })}>
            <option value="">Choose a project</option>
            {projects.map((project) => (
              <option key={project.project_id} value={project.project_id}>
                {project.project_id}{project.preferred_provider ? " (prefers " + project.preferred_provider + ")" : ""}
              </option>
            ))}
          </select>
        </label>
        {PREFERENCE.map((option) => (
          <Radio key={option.value || "unchanged"} name={id + "-preference"} checked={draft.preferenceMode === option.value}
            onChange={() => set({ preferenceMode: option.value })}>{option.label}</Radio>
        ))}
      </Fieldset>

      <Fieldset legend="Drain" hint="What happens to live workers of profiles that leave the pool or lose bounds. New claims stop at once either way.">
        <Radio name={id + "-drain"} checked={draft.drain === "graceful"} onChange={() => setDrain("graceful")}>
          Graceful — idle workers stop; busy workers finish their task first
        </Radio>
        <Radio name={id + "-drain"} checked={draft.drain === "idle-now"} onChange={() => setDrain("idle-now")}>
          Stop idle workers now — busy workers still finish their task
        </Radio>
        <button type="button" aria-expanded={dangerOpen} onClick={() => setDangerOpen(!dangerOpen)}
          className="mt-1 flex items-center gap-1.5 text-xs text-red-300 hover:underline">
          <ExclamationTriangleIcon className="h-3.5 w-3.5" />Danger: interrupt busy work
        </button>
        {dangerOpen && (
          <div className="rounded border border-red-900/70 bg-red-950/20 p-2">
            <Radio name={id + "-drain"} checked={draft.drain === "interrupt-busy"} onChange={() => setDrain("interrupt-busy")}>
              Interrupt busy work — stop busy workers mid-task (operator only; you confirm the exact set next)
            </Radio>
          </div>
        )}
      </Fieldset>

      {error && <div role="alert" className="rounded border border-red-900 bg-red-950/30 p-3 text-sm text-red-300">{error}</div>}
      {problem && <p className="text-xs text-amber-300">{problem}</p>}
      <div className="flex justify-end">
        <button type="button" disabled={!!problem || pending} onClick={onPreview}
          className="rounded bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-40">
          {pending ? "Previewing…" : "Preview"}
        </button>
      </div>
    </div>
  );
}
