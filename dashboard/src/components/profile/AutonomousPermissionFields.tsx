import { ExclamationTriangleIcon } from "@heroicons/react/24/outline";
import type { ReactNode } from "react";
import type { ProfileFormState } from "./profileForm";

interface Props {
  form: ProfileFormState;
  onChange: <K extends keyof ProfileFormState>(
    key: K,
    value: ProfileFormState[K],
  ) => void;
}

export default function AutonomousPermissionFields({ form, onChange }: Props) {
  if (form.harness === "codex") {
    const legacyBypass = form.permission_mode === "bypassPermissions";
    return (
      <WarningCheckbox
        label="Codex full auto"
        checked={form.codex_full_auto}
        onChange={(checked) => onChange("codex_full_auto", checked)}
        disabled={legacyBypass}
        dangerous={legacyBypass}
      >
        {legacyBypass
          ? "Legacy bypassPermissions disables approvals and sandbox restrictions, and takes precedence over full auto. Clear Permission mode to use sandboxed full auto."
          : "Allows Codex to edit files and run commands without asking for approval inside its workspace sandbox. It does not disable sandbox restrictions."}
      </WarningCheckbox>
    );
  }

  if (form.harness === "claude") {
    return (
      <WarningCheckbox
        label="Claude dangerously skip permissions"
        checked={form.claude_dangerously_skip_permissions}
        onChange={(checked) =>
          onChange("claude_dangerously_skip_permissions", checked)
        }
        dangerous
      >
        Disables all permission prompts and lets Claude execute tools without confirmation.
        Only enable this in a fully trusted, isolated environment.
      </WarningCheckbox>
    );
  }

  return null;
}

function WarningCheckbox({
  label,
  checked,
  onChange,
  disabled = false,
  dangerous = false,
  children,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  dangerous?: boolean;
  children: ReactNode;
}) {
  const containerClass = dangerous
    ? "rounded-md border border-red-500/30 bg-red-500/5 p-3"
    : "rounded-md border border-amber-500/30 bg-amber-500/5 p-3";
  const inputClass = dangerous
    ? "mt-0.5 h-4 w-4 cursor-pointer rounded border-gray-700 bg-gray-900 accent-red-500 disabled:cursor-not-allowed disabled:opacity-60"
    : "mt-0.5 h-4 w-4 cursor-pointer rounded border-gray-700 bg-gray-900 accent-amber-500 disabled:cursor-not-allowed disabled:opacity-60";
  const labelClass = dangerous
    ? "flex items-center gap-1.5 text-sm text-red-200"
    : "flex items-center gap-1.5 text-sm text-amber-200";
  return (
    <div className={containerClass}>
      <label className="flex items-start gap-2">
        <input
          aria-label={label}
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
          className={inputClass}
        />
        <div className="space-y-1">
          <span className={labelClass}>
            <ExclamationTriangleIcon className="h-4 w-4 shrink-0" />
            {label}
          </span>
          <p className="text-xs text-gray-400">{children}</p>
        </div>
      </label>
    </div>
  );
}
