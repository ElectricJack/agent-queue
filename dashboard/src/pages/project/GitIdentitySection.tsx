import { useId } from "react";
import { Link } from "react-router-dom";
import { ExclamationTriangleIcon } from "@heroicons/react/24/outline";
import type { EffectiveGitIdentity } from "../../api/client";
import {
  GIT_IDENTITY_APPLIES_NOTE,
  GIT_IDENTITY_CLI_HINT,
  GIT_IDENTITY_SETTINGS_PATH,
  GIT_IDENTITY_SOURCE_LABEL,
  type GitIdentityFieldErrors,
  type GitIdentityFormFields,
  type GitIdentitySource,
  inheritedGitIdentity,
} from "./gitIdentity";

const SOURCE_BADGE: Record<GitIdentitySource, string> = {
  project: "border-indigo-500/30 bg-indigo-500/10 text-indigo-300",
  installation: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
  fallback: "border-amber-500/40 bg-amber-500/10 text-amber-300",
};

const INPUT_CLASS =
  "w-full rounded-md border bg-gray-950 px-3 py-1.5 text-sm text-gray-200 focus:outline-none";

/**
 * The project's Git commit identity: the effective pair and where it comes
 * from, plus the override / reset controls. Every change goes through
 * `onChange`; the host decides when it is saved (Config.tsx enters edit
 * mode, ProjectSubject marks its form dirty).
 */
export default function GitIdentitySection({
  identity,
  value,
  onChange,
  editing,
  errors,
}: {
  /** The saved, effective identity from getProject (`git_identity`). */
  identity: EffectiveGitIdentity | null | undefined;
  value: GitIdentityFormFields;
  onChange: (next: GitIdentityFormFields) => void;
  /** Whether the override inputs are editable (always true in the pane). */
  editing: boolean;
  errors: GitIdentityFieldErrors;
}) {
  const id = useId();
  const inherited = inheritedGitIdentity(identity);
  const override = value.git_identity_override;
  const unconfigured = identity != null && !identity.installation;

  const startOverride = () =>
    onChange({
      git_identity_override: true,
      git_identity_name: value.git_identity_name || inherited?.name || "",
      git_identity_email: value.git_identity_email || inherited?.email || "",
    });
  const reset = () =>
    onChange({
      git_identity_override: false,
      git_identity_name: inherited?.name ?? "",
      git_identity_email: inherited?.email ?? "",
    });

  return (
    <div className="space-y-3 text-sm" data-testid="git-identity-section">
      {identity ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-gray-200" data-testid="git-identity-effective">
            {identity.name} &lt;{identity.email}&gt;
          </span>
          <span
            className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${SOURCE_BADGE[identity.source]}`}
            data-testid="git-identity-source"
          >
            {identity.source === "fallback" && <ExclamationTriangleIcon className="h-3.5 w-3.5" />}
            {GIT_IDENTITY_SOURCE_LABEL[identity.source]}
          </span>
        </div>
      ) : (
        <p className="text-gray-500">The daemon did not report this project's effective identity.</p>
      )}

      {identity?.source === "fallback" && (
        <div
          role="note"
          className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-200"
        >
          <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            No installation default is configured, so commits use AQ's placeholder identity. Set
            one in{" "}
            <Link to={GIT_IDENTITY_SETTINGS_PATH} className="underline hover:text-amber-100">
              Settings › Config
            </Link>{" "}
            (<code>git_identity</code>) or with <code>{GIT_IDENTITY_CLI_HINT}</code>.
          </span>
        </div>
      )}

      {override ? (
        <div className="space-y-3">
          {editing && (
            <>
              <IdentityInput
                id={`${id}-name`}
                label="Git commit name"
                value={value.git_identity_name}
                error={errors.name}
                placeholder="Jane Doe"
                onChange={(v) => onChange({ ...value, git_identity_name: v })}
              />
              <IdentityInput
                id={`${id}-email`}
                label="Git commit email"
                value={value.git_identity_email}
                error={errors.email}
                placeholder="jane@example.com"
                onChange={(v) => onChange({ ...value, git_identity_email: v })}
              />
            </>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={reset}
              className="rounded-md border border-gray-700 bg-gray-800 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-700"
            >
              Reset to installation default
            </button>
            {inherited && (
              <span className="text-xs text-gray-500">
                Inherits {inherited.name} &lt;{inherited.email}&gt;
                {unconfigured && " (fallback — installation default not configured)"}
              </span>
            )}
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          {identity?.source === "project" && inherited && (
            <p className="text-xs text-gray-400" data-testid="git-identity-pending-reset">
              After saving, commits use {inherited.name} &lt;{inherited.email}&gt; ·{" "}
              {GIT_IDENTITY_SOURCE_LABEL[inherited.source]}
            </p>
          )}
          <button
            type="button"
            onClick={startOverride}
            className="rounded-md border border-gray-700 bg-gray-800 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-700"
          >
            Override for this project
          </button>
        </div>
      )}

      {errors.general && (
        <p role="alert" className="text-xs text-red-400">
          {errors.general}
        </p>
      )}
      <p className="text-xs text-gray-500">{GIT_IDENTITY_APPLIES_NOTE}</p>
    </div>
  );
}

function IdentityInput({
  id,
  label,
  value,
  error,
  placeholder,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  error?: string;
  placeholder?: string;
  onChange: (v: string) => void;
}) {
  return (
    <div>
      <label htmlFor={id} className="mb-1 block text-xs font-medium text-gray-400">
        {label}
      </label>
      <input
        id={id}
        type="text"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-error` : undefined}
        className={`${INPUT_CLASS} ${
          error ? "border-red-500/60 focus:border-red-500" : "border-gray-700 focus:border-indigo-500"
        }`}
      />
      {error && (
        <p id={`${id}-error`} role="alert" className="mt-1 text-xs text-red-400">
          {error}
        </p>
      )}
    </div>
  );
}
