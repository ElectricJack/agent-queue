import { useEffect } from "react";
import { ArrowTopRightOnSquareIcon } from "@heroicons/react/24/outline";
import { useNavigate } from "react-router-dom";
import { useIntelligenceClasses } from "../../../api/hooks";
import type { PaneViewProps } from "../../types";
import type { ContextualSettingsArgs } from "../args";

type Args = Extract<ContextualSettingsArgs, { subject: "intelligence-class" }>;

export default function IntelligenceClassSubject({ args, setToolbar }: PaneViewProps<Args>) {
  const { data, isLoading, error } = useIntelligenceClasses();
  const navigate = useNavigate();
  const cls = data?.classes.find((c) => c.id === args.subjectId);

  useEffect(() => {
    setToolbar([
      {
        id: "open-full",
        label: "Open full settings page",
        icon: ArrowTopRightOnSquareIcon,
        onClick: () => navigate("/settings/intelligence-classes"),
      },
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (isLoading) return <p className="text-sm text-gray-500">Loading intelligence class…</p>;
  if (error) return <p className="text-sm text-red-400">{(error as Error).message}</p>;
  if (!cls) return <p className="text-sm text-gray-500">Intelligence class "{args.subjectId}" not found.</p>;

  return (
    <div className="space-y-3 text-sm">
      <div>
        <p className="font-medium text-gray-100">{cls.name}</p>
        <p className="text-xs text-gray-400">{cls.description}</p>
      </div>
      <ul className="space-y-1 font-mono text-xs text-gray-400">
        {Object.entries(cls.mapping)
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([provider, slice]) => (
            <li key={provider}>
              <span className="text-gray-500">{provider}:</span> {slice.model ?? "—"}
              {slice.thinking ? ` · think:${slice.thinking}` : ""}
            </li>
          ))}
      </ul>
      <p className="text-xs text-gray-500">
        These classes ship from the vault. Edit{" "}
        <code className="rounded bg-gray-800 px-1">vault/intelligence-classes/{args.subjectId}.md</code>{" "}
        to change them.
      </p>
    </div>
  );
}
