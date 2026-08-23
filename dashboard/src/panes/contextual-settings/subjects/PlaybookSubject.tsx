import { useEffect, useState } from "react";
import { CheckIcon, ArrowUturnLeftIcon, ArrowTopRightOnSquareIcon } from "@heroicons/react/24/outline";
import { useNavigate } from "react-router-dom";
import { usePlaybookSource, useUpdatePlaybookSource, usePlaybooks, type PlaybookUpdateResult } from "../../../api/hooks";
import { fullSettingsRoute } from "../fullSettingsRoute";
import type { PaneViewProps } from "../../types";
import type { ContextualSettingsArgs } from "../args";

type Args = Extract<ContextualSettingsArgs, { subject: "playbook" }>;

export default function PlaybookSubject({ args, setToolbar }: PaneViewProps<Args>) {
  const navigate = useNavigate();
  const { data: source, isLoading } = usePlaybookSource(args.subjectId);
  const { data: playbooks } = usePlaybooks();
  const update = useUpdatePlaybookSource();
  const meta = playbooks?.find((p) => p.id === args.subjectId);

  const [draft, setDraft] = useState("");
  const [baseHash, setBaseHash] = useState("");
  const [lastResult, setLastResult] = useState<PlaybookUpdateResult | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (source) {
      setDraft(source.markdown);
      setBaseHash(source.source_hash);
      setLastResult(null);
      setSaveError(null);
    }
  }, [source]);

  const dirty = source ? draft !== source.markdown : false;

  const save = async () => {
    setSaveError(null);
    setLastResult(null);
    try {
      const result = await update.mutateAsync({
        playbook_id: args.subjectId,
        markdown: draft,
        expected_source_hash: baseHash,
      });
      setLastResult(result);
      if (result.source_hash) setBaseHash(result.source_hash);
      if (result.error === "conflict") {
        setSaveError(
          "Vault changed underneath this editor. Reload to pick up the latest, or overwrite by saving again without the hash.",
        );
      }
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => {
    setToolbar([
      { id: "save", label: "Save", icon: CheckIcon, onClick: save, disabled: !dirty || update.isPending },
      {
        id: "discard",
        label: "Discard changes",
        icon: ArrowUturnLeftIcon,
        onClick: () => source && setDraft(source.markdown),
        disabled: !dirty,
      },
      {
        id: "open-full",
        label: "Open full settings page",
        icon: ArrowTopRightOnSquareIcon,
        onClick: () => navigate(fullSettingsRoute(args)),
      },
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty, update.isPending, draft, source]);

  if (isLoading) return <p className="text-sm text-gray-500">Loading source…</p>;
  if (!source) return <p className="text-sm text-gray-500">Source unavailable.</p>;

  return (
    <div className="space-y-3 text-sm">
      {meta && (
        <div className="flex flex-wrap items-center gap-3 text-xs text-gray-400">
          <span>{meta.scope}{meta.scope_identifier ? `:${meta.scope_identifier}` : ""}</span>
          <span>v{meta.version}</span>
          <span>{meta.node_count} nodes</span>
          {(meta.triggers ?? []).map((t) => (
            <span key={t} className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">{t}</span>
          ))}
        </div>
      )}

      <textarea
        aria-label="Playbook source"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        spellCheck={false}
        className="h-[50vh] w-full resize-none rounded-lg border border-gray-800 bg-gray-900 p-3 font-mono text-xs text-gray-200 focus:border-indigo-500 focus:outline-none"
      />

      {saveError && <p className="text-xs text-red-400">{saveError}</p>}

      {lastResult && lastResult.compiled && (
        <p className="text-xs text-emerald-300">
          Compiled v{lastResult.version} — {lastResult.node_count} nodes.
        </p>
      )}

      {lastResult && !lastResult.compiled && lastResult.errors && (
        <div className="text-xs text-amber-200">
          <p>Validation failed — previous compiled version still live.</p>
          <ul className="ml-4 list-disc">
            {lastResult.errors.map((e, i) => (
              <li key={i} className="font-mono">{e}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
