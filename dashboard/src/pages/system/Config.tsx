import { useEffect, useMemo, useState } from "react";
import {
  ArrowPathIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  KeyIcon,
} from "@heroicons/react/24/outline";

import {
  useReloadSystemConfig,
  useSystemConfig,
  useSystemConfigSchema,
  useUpdateSystemConfig,
} from "../../api/hooks";

type ReloadKind = "hot" | "restart" | "unclassified";

interface Toast {
  level: "success" | "warning" | "error";
  text: string;
}

export default function SystemConfig() {
  const { data: configData, isLoading: configLoading, error: configError } = useSystemConfig();
  const { data: schemaData } = useSystemConfigSchema();
  const reload = useReloadSystemConfig();
  const update = useUpdateSystemConfig();

  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [draftError, setDraftError] = useState<string | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);

  const allSections = useMemo(() => {
    if (!configData) return [] as string[];
    return Object.keys(configData.config ?? {}).sort();
  }, [configData]);

  // Default-select the first section once the data lands.
  useEffect(() => {
    const first = allSections[0];
    if (selected === null && first) {
      setSelected(first);
    }
  }, [allSections, selected]);

  // Re-seed the draft whenever the selected section's data changes.
  useEffect(() => {
    if (!selected || !configData) return;
    const sectionVal = (configData.config ?? {})[selected];
    setDraft(JSON.stringify(sectionVal ?? null, null, 2));
    setDraftError(null);
  }, [selected, configData]);

  const reloadByName = useMemo(() => buildReloadIndex(configData, schemaData), [configData, schemaData]);
  const refsBySection = useMemo(() => buildRefIndex(configData), [configData]);

  if (configError) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-bold">System Config</h1>
        <div className="rounded-lg border border-red-900/40 bg-red-950/30 p-4 text-sm text-red-200">
          Failed to load config: {(configError as Error).message}
        </div>
      </div>
    );
  }

  const path = configData?.path ?? "~/.agent-queue/config.yaml";
  const selectedKind: ReloadKind =
    (selected ? reloadByName[selected] : undefined) ?? "unclassified";
  const selectedRefs: ConfigEnvRef[] = selected ? (refsBySection[selected] ?? []) : [];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">System Config</h1>
          <p className="mt-1 text-xs text-gray-500">{path}</p>
        </div>
        <button
          type="button"
          disabled={reload.isPending}
          onClick={async () => {
            try {
              const r = await reload.mutateAsync();
              const applied = r.applied ?? [];
              setToast({
                level: "success",
                text: r.message
                  ? `${r.message}${applied.length ? ` Applied: ${applied.join(", ")}` : ""}`
                  : "Reloaded.",
              });
            } catch (e) {
              setToast({ level: "error", text: (e as Error).message });
            }
          }}
          className="inline-flex items-center gap-1.5 rounded-md border border-gray-700 bg-gray-900 px-3 py-1.5 text-sm font-medium text-gray-200 hover:bg-gray-800"
        >
          <ArrowPathIcon className={`h-4 w-4 ${reload.isPending ? "animate-spin" : ""}`} />
          Reload from disk
        </button>
      </div>

      {toast && <ToastBanner toast={toast} onDismiss={() => setToast(null)} />}

      {configLoading || !configData ? (
        <p className="text-sm text-gray-500">Loading...</p>
      ) : (
        <div className="grid grid-cols-12 gap-4">
          <aside className="col-span-4 lg:col-span-3 space-y-1">
            {allSections.map((name) => {
              const kind = reloadByName[name] ?? "unclassified";
              const refs = refsBySection[name] ?? [];
              const isActive = name === selected;
              return (
                <button
                  key={name}
                  type="button"
                  onClick={() => setSelected(name)}
                  className={`flex w-full items-center justify-between rounded-md px-3 py-1.5 text-left text-sm transition-colors ${
                    isActive
                      ? "bg-indigo-600/20 text-indigo-200"
                      : "text-gray-300 hover:bg-gray-800/60"
                  }`}
                >
                  <span className="truncate font-mono">{name}</span>
                  <span className="ml-2 flex shrink-0 items-center gap-1.5">
                    {refs.length > 0 && (
                      <span title={`${refs.length} ${"${ENV_VAR}"} reference${refs.length === 1 ? "" : "s"}`}>
                        <KeyIcon className={`h-3.5 w-3.5 ${refs.some(r => !r.resolved) ? "text-amber-400" : "text-gray-500"}`} />
                      </span>
                    )}
                    <ReloadBadge kind={kind} />
                  </span>
                </button>
              );
            })}
          </aside>

          <main className="col-span-8 lg:col-span-9 space-y-4">
            {selected ? (
              <>
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <h2 className="text-lg font-semibold font-mono">{selected}</h2>
                    <ReloadBadge kind={selectedKind} verbose />
                  </div>
                </div>

                {selectedRefs.length > 0 && (
                  <div className="rounded-md border border-gray-800 bg-gray-900/40 p-3 text-xs text-gray-400">
                    <p className="mb-1.5 font-medium text-gray-300">Environment variable references</p>
                    <ul className="space-y-0.5">
                      {selectedRefs.map((r) => (
                        <li key={r.path} className="flex items-center gap-2 font-mono">
                          {r.resolved ? (
                            <CheckCircleIcon className="h-3.5 w-3.5 text-emerald-400" />
                          ) : (
                            <ExclamationTriangleIcon className="h-3.5 w-3.5 text-amber-400" />
                          )}
                          <span className="text-gray-300">{r.path}</span>
                          <span className="text-gray-500">→</span>
                          <span className="text-gray-300">${`{${r.var}}`}</span>
                          {!r.resolved && <span className="text-amber-400">(unresolved)</span>}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                <textarea
                  value={draft}
                  onChange={(e) => {
                    setDraft(e.target.value);
                    setDraftError(null);
                  }}
                  spellCheck={false}
                  className="block h-96 w-full rounded-md border border-gray-800 bg-gray-950 p-3 font-mono text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
                />

                {draftError && (
                  <div className="rounded-md border border-red-900/40 bg-red-950/30 p-2 text-xs text-red-200">
                    {draftError}
                  </div>
                )}

                <div className="flex items-center justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      const sectionVal = (configData.config ?? {})[selected];
                      setDraft(JSON.stringify(sectionVal ?? null, null, 2));
                      setDraftError(null);
                    }}
                    className="rounded-md border border-gray-700 bg-gray-900 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-800"
                  >
                    Discard
                  </button>
                  <button
                    type="button"
                    disabled={update.isPending}
                    onClick={async () => {
                      let parsed: unknown;
                      try {
                        parsed = JSON.parse(draft);
                      } catch (e) {
                        setDraftError(`Invalid JSON: ${(e as Error).message}`);
                        return;
                      }
                      try {
                        const r = await update.mutateAsync({
                          section: selected,
                          data: parsed,
                        });
                        if (r.validation_errors && r.validation_errors.length > 0) {
                          setDraftError(r.validation_errors.join("\n"));
                          return;
                        }
                        if (r.requires_restart) {
                          setToast({
                            level: "warning",
                            text: `Saved — restart required for "${selected}" to take effect.`,
                          });
                        } else {
                          setToast({ level: "success", text: `Saved + applied live: ${selected}` });
                        }
                      } catch (e) {
                        setDraftError((e as Error).message);
                      }
                    }}
                    className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-60"
                  >
                    {update.isPending ? "Saving..." : "Save"}
                  </button>
                </div>
              </>
            ) : (
              <p className="text-sm text-gray-500">Select a section to view or edit.</p>
            )}
          </main>
        </div>
      )}
    </div>
  );
}

function ReloadBadge({ kind, verbose = false }: { kind: ReloadKind; verbose?: boolean }) {
  const cfg =
    kind === "hot"
      ? { text: verbose ? "Live reload" : "live", cls: "bg-emerald-900/40 text-emerald-300" }
      : kind === "restart"
        ? { text: verbose ? "Restart required" : "restart", cls: "bg-amber-900/40 text-amber-300" }
        : { text: verbose ? "Unclassified" : "?", cls: "bg-gray-800 text-gray-400" };
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${cfg.cls}`}>
      {cfg.text}
    </span>
  );
}

function ToastBanner({ toast, onDismiss }: { toast: Toast; onDismiss: () => void }) {
  const cls =
    toast.level === "success"
      ? "border-emerald-900/40 bg-emerald-950/30 text-emerald-200"
      : toast.level === "warning"
        ? "border-amber-900/40 bg-amber-950/30 text-amber-200"
        : "border-red-900/40 bg-red-950/30 text-red-200";
  return (
    <div className={`flex items-start justify-between rounded-md border p-3 text-sm ${cls}`}>
      <span>{toast.text}</span>
      <button type="button" onClick={onDismiss} className="ml-3 text-xs underline opacity-80 hover:opacity-100">
        Dismiss
      </button>
    </div>
  );
}

interface ConfigEnvRef {
  path: string;
  var: string;
  resolved: boolean;
}

function buildReloadIndex(
  configData: ReturnType<typeof useSystemConfig>["data"],
  schemaData: ReturnType<typeof useSystemConfigSchema>["data"],
): Record<string, ReloadKind> {
  const out: Record<string, ReloadKind> = {};
  if (configData) {
    for (const name of configData.hot_reloadable ?? []) out[name] = "hot";
    for (const name of configData.restart_required ?? []) out[name] = "restart";
    for (const name of configData.unclassified ?? []) out[name] = "unclassified";
  }
  // Schema annotations are authoritative when present.
  const props = (schemaData?.schema as { properties?: Record<string, { "x-reload"?: ReloadKind }> } | undefined)
    ?.properties;
  if (props) {
    for (const [name, prop] of Object.entries(props)) {
      const k = prop["x-reload"];
      if (k) out[name] = k;
    }
  }
  return out;
}

function buildRefIndex(
  configData: ReturnType<typeof useSystemConfig>["data"],
): Record<string, ConfigEnvRef[]> {
  const out: Record<string, ConfigEnvRef[]> = {};
  for (const ref of (configData?.env_var_references ?? []) as ConfigEnvRef[]) {
    const section = ref.path.split(".")[0]?.split("[")[0];
    if (!section) continue;
    if (!out[section]) out[section] = [];
    out[section]!.push(ref);
  }
  return out;
}
