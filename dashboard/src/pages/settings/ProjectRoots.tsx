import { useEffect, useState } from "react";

import {
  useSystemConfig,
  useSystemConfigSchema,
  useUpdateSystemConfig,
} from "../../api/hooks";

interface ProjectRoot {
  id: string;
  label: string;
  path: string;
}

interface ProjectRootCapability extends ProjectRoot {
  readable: boolean;
  writable: boolean;
}

interface FieldSchema {
  title?: string;
  description?: string;
}

const ROOT_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

function asRoots(value: unknown): ProjectRoot[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) => {
    if (!entry || typeof entry !== "object") return [];
    const root = entry as Record<string, unknown>;
    return typeof root.id === "string" && typeof root.label === "string" && typeof root.path === "string"
      ? [{ id: root.id, label: root.label, path: root.path }]
      : [];
  });
}

function validateRoots(roots: ProjectRoot[]): Record<string, string> {
  const errors: Record<string, string> = {};
  const ids = new Set<string>();
  roots.forEach((root, index) => {
    if (!ROOT_ID.test(root.id)) errors[`${index}.id`] = "Use letters, digits, '.', '_' or '-' and start with a letter or digit.";
    else if (ids.has(root.id)) errors[`${index}.id`] = "Each root ID must be unique.";
    ids.add(root.id);
    if (!root.label.trim()) errors[`${index}.label`] = "A label is required.";
    if (!root.path.trim()) errors[`${index}.path`] = "A path is required.";
  });
  return errors;
}

function splitServerValidationErrors(messages: string[]): [Record<string, string>, string[]] {
  const fields: Record<string, string> = {};
  const other: string[] = [];
  for (const message of messages) {
    const match = /^project_roots\[(\d+)\]\.(id|label|path):\s*(.+)$/.exec(message);
    if (match) fields[`${match[1]}.${match[2]}`] = match[3]!;
    else other.push(message);
  }
  return [fields, other];
}

async function listCapabilities(): Promise<Map<string, ProjectRootCapability>> {
  const response = await fetch("/api/project/list-roots", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  if (!response.ok) return new Map();
  const payload: unknown = await response.json();
  if (!payload || typeof payload !== "object" || !Array.isArray((payload as { roots?: unknown }).roots)) {
    return new Map();
  }
  return new Map(asRoots((payload as { roots: unknown }).roots).flatMap((root) => {
    const source = (payload as { roots: Array<Record<string, unknown>> }).roots.find((item) => item.id === root.id);
    return source && typeof source.readable === "boolean" && typeof source.writable === "boolean"
      ? [[root.id, { ...root, readable: source.readable, writable: source.writable }] as const]
      : [];
  }));
}

export default function ProjectRoots() {
  const config = useSystemConfig();
  const schema = useSystemConfigSchema();
  const update = useUpdateSystemConfig();
  const [roots, setRoots] = useState<ProjectRoot[]>([]);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [serverError, setServerError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [removeIndex, setRemoveIndex] = useState<number | null>(null);
  const [capabilities, setCapabilities] = useState<Map<string, ProjectRootCapability>>(new Map());

  const projectRootsSchema = (schema.data?.schema as {
    properties?: Record<string, { items?: { properties?: Record<string, FieldSchema> } }>;
  } | undefined)?.properties?.project_roots;
  const fields = projectRootsSchema?.items?.properties ?? {};

  useEffect(() => {
    setRoots(asRoots((config.data?.config as Record<string, unknown> | undefined)?.project_roots));
    setErrors({});
    setServerError(null);
  }, [config.data]);

  useEffect(() => {
    let cancelled = false;
    void listCapabilities().then((next) => {
      if (!cancelled && next.size) setCapabilities(next);
    }).catch(() => {
      // The onboarding API may not have landed yet. Config editing remains usable without badges.
    });
    return () => { cancelled = true; };
  }, []);

  const replaceRoot = (index: number, field: keyof ProjectRoot, value: string) => {
    setRoots((current) => current.map((root, rootIndex) => rootIndex === index ? { ...root, [field]: value } : root));
    setErrors({});
    setServerError(null);
    setNotice(null);
  };
  const removeRoot = () => {
    if (removeIndex === null) return;
    setRoots((current) => current.filter((_, index) => index !== removeIndex));
    setRemoveIndex(null);
    setErrors({});
    setServerError(null);
    setNotice(null);
  };
  const save = async () => {
    const nextErrors = validateRoots(roots);
    if (Object.keys(nextErrors).length) {
      setErrors(nextErrors);
      return;
    }
    setServerError(null);
    setNotice(null);
    try {
      const result = await update.mutateAsync({ section: "project_roots", data: roots });
      if (result.validation_errors?.length) {
        const [fieldErrors, otherErrors] = splitServerValidationErrors(result.validation_errors);
        setErrors(fieldErrors);
        setServerError(otherErrors.length ? otherErrors.join("\n") : null);
        return;
      }
      setNotice(result.requires_restart ? "Saved. Restart required to apply this change." : "Project roots saved.");
    } catch (error) {
      setServerError(error instanceof Error ? error.message : String(error));
    }
  };

  if (config.error) {
    return <div className="rounded-lg border border-red-900/40 bg-red-950/30 p-4 text-sm text-red-200">Failed to load project roots: {(config.error as Error).message}</div>;
  }
  if (config.isLoading || schema.isLoading) return <p className="text-sm text-gray-500">Loading project roots…</p>;
  if (!projectRootsSchema) {
    return <div className="rounded-lg border border-amber-900/40 bg-amber-950/30 p-4 text-sm text-amber-200">Project roots are unavailable until this daemon supports root configuration.</div>;
  }

  return (
    <section className="space-y-5">
      <header>
        <h1 className="text-2xl font-bold">Project roots</h1>
        <p className="mt-1 text-sm text-gray-500">Directories the daemon may use for browsing and project onboarding.</p>
      </header>
      {notice && <div role="status" className="rounded-md border border-emerald-900/40 bg-emerald-950/30 p-3 text-sm text-emerald-200">{notice}</div>}
      {serverError && <div role="alert" className="whitespace-pre-line rounded-md border border-red-900/40 bg-red-950/30 p-3 text-sm text-red-200">{serverError}</div>}
      <div className="space-y-4">
        {roots.map((root, index) => {
          const capability = capabilities.get(root.id);
          return <fieldset key={index} className="rounded-lg border border-gray-800 bg-gray-900/40 p-4">
            <legend className="px-1 text-sm font-medium text-gray-200">{root.label || "New project root"}</legend>
            <div className="grid gap-4 md:grid-cols-3">
              {(["id", "label", "path"] as const).map((field) => {
                const inputId = `project-root-${index}-${field}`;
                const message = errors[`${index}.${field}`];
                return <label key={field} htmlFor={inputId} className="block text-xs text-gray-400">
                  {fields[field]?.title ?? (field === "id" ? "ID" : field[0]!.toUpperCase() + field.slice(1))}
                  <input id={inputId} value={root[field]} aria-invalid={!!message} aria-describedby={message ? inputId + "-error" : undefined}
                    onChange={(event) => replaceRoot(index, field, event.target.value)}
                    className="mt-1 block w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none" />
                  {message && <span id={inputId + "-error"} role="alert" className="mt-1 block text-red-300">{message}</span>}
                </label>;
              })}
            </div>
            <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
              <div className="flex gap-2 text-xs">
                {capability && <><CapabilityBadge label="Readable" enabled={capability.readable} /><CapabilityBadge label="Writable" enabled={capability.writable} /></>}
              </div>
              <button type="button" onClick={() => setRemoveIndex(index)} className="rounded-md border border-red-900/60 px-3 py-1.5 text-sm text-red-300 hover:bg-red-950/40">Remove {root.id || "root"}</button>
            </div>
          </fieldset>;
        })}
      </div>
      <div className="flex flex-wrap justify-between gap-3">
        <button type="button" onClick={() => { setRoots((current) => [...current, { id: "", label: "", path: "" }]); setNotice(null); }} className="rounded-md border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-gray-200 hover:bg-gray-800">Add project root</button>
        <button type="button" disabled={update.isPending} onClick={() => void save()} className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-60">{update.isPending ? "Saving…" : "Save project roots"}</button>
      </div>
      {removeIndex !== null && <div role="dialog" aria-modal="true" aria-labelledby="remove-root-title" className="rounded-lg border border-amber-900/50 bg-amber-950/30 p-4">
        <h2 id="remove-root-title" className="font-medium text-amber-100">Remove project root?</h2>
        <p className="mt-2 text-sm text-amber-100/80">Existing projects beneath this root are unaffected, but new browsing and onboarding will no longer be possible there.</p>
        <div className="mt-4 flex justify-end gap-2"><button type="button" onClick={() => setRemoveIndex(null)} className="rounded-md border border-gray-700 px-3 py-1.5 text-sm text-gray-200">Cancel</button><button type="button" onClick={removeRoot} className="rounded-md bg-red-700 px-3 py-1.5 text-sm font-medium text-white">Remove root</button></div>
      </div>}
    </section>
  );
}

function CapabilityBadge({ label, enabled }: { label: string; enabled: boolean }) {
  return <span className={enabled ? "rounded bg-emerald-900/40 px-2 py-1 text-emerald-300" : "rounded bg-red-900/40 px-2 py-1 text-red-300"}>{label}: {enabled ? "yes" : "no"}</span>;
}
