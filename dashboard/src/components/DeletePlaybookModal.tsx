import { useEffect, useMemo, useState } from "react";
import { ExclamationTriangleIcon } from "@heroicons/react/24/outline";
import Modal from "./Modal";
import { useDeletePlaybook, usePlaybookActivationHealth } from "../api/hooks";

interface Props {
  open: boolean;
  onClose: () => void;
  playbookId: string;
  /** Scope of the row the operator clicked, when the caller knows it. Without
   *  it the dialog can only act when the playbook is installed in exactly one
   *  scope — deleting the wrong scope's entry is not recoverable. */
  scope?: string;
  scopeIdentifier?: string | null;
  /** Called after the entry is gone, for the caller to navigate or close. */
  onDeleted?: () => void;
}

interface Activation {
  playbook_id: string;
  scope: string;
  scope_identifier?: string | null;
  enabled?: boolean;
  active_artifact_sha256?: string | null;
  running_count?: number;
  pending_event_count?: number;
}

function scopeLabel(scope: string, identifier?: string | null) {
  return identifier ? `${scope}:${identifier}` : scope;
}

/** Confirm-and-delete for one installed playbook entry.
 *
 *  The server keys the delete on the exact (scope, scope_identifier,
 *  artifact_sha256) triple and refuses an enabled entry, so the hash is read
 *  from activation health at dialog-open time rather than from a list row that
 *  may have been rendered minutes ago.  Every precondition the server enforces
 *  is stated here before the operator commits, and a refusal is shown in place
 *  with the row left alone. */
export default function DeletePlaybookModal({
  open,
  onClose,
  playbookId,
  scope,
  scopeIdentifier,
  onDeleted,
}: Props) {
  const del = useDeletePlaybook();
  const { data, isLoading } = usePlaybookActivationHealth(open ? playbookId : undefined);
  const [fatal, setFatal] = useState<string | null>(null);

  useEffect(() => {
    if (!open) setFatal(null);
  }, [open]);

  const rows = useMemo(
    () => ((data?.activations ?? []) as Activation[]).filter((a) => a.playbook_id === playbookId),
    [data, playbookId],
  );
  const target = useMemo(() => {
    if (scope) {
      const wanted = scopeIdentifier ?? "";
      return rows.find((a) => a.scope === scope && (a.scope_identifier ?? "") === wanted) ?? null;
    }
    return rows.length === 1 ? rows[0] : null;
  }, [rows, scope, scopeIdentifier]);

  const ambiguous = !scope && rows.length > 1;
  const sha = target?.active_artifact_sha256 ?? "";
  const stillEnabled = target?.enabled === true;
  const busyCount = (target?.running_count ?? 0) + (target?.pending_event_count ?? 0);

  const blocker = isLoading
    ? "Reading this playbook's installed entry…"
    : ambiguous
      ? "This playbook is installed in more than one scope. Delete it from its row in the playbook list so the scope is unambiguous."
      : !target
        ? "No installed entry for this playbook. It may already have been deleted."
        : stillEnabled
          ? "This playbook is enabled. Pause its triggers first — the daemon refuses to delete an enabled entry."
          : !sha
            ? "This entry records no artifact hash, so it cannot be deleted by hash. Reload the page and try again."
            : null;

  const onConfirm = async () => {
    if (!target || !sha) return;
    setFatal(null);
    try {
      await del.mutateAsync({
        playbook_id: playbookId,
        scope: target.scope,
        scope_identifier: target.scope_identifier ?? "",
        artifact_sha256: sha,
      });
      onClose();
      onDeleted?.();
    } catch (err) {
      setFatal(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Delete playbook">
      <div className="space-y-4">
        <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">
          <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
          <div className="space-y-1">
            <p>
              This removes <span className="font-mono">{playbookId}</span> (
              {scopeLabel(target?.scope ?? scope ?? "unknown scope", target?.scope_identifier ?? scopeIdentifier)}
              ) from the installed catalog.
            </p>
            <p className="text-xs text-red-300/80">
              Its Markdown source stays in the vault; recorded runs are kept.
            </p>
          </div>
        </div>

        {sha && (
          <p className="break-all font-mono text-xs text-gray-500">artifact {sha}</p>
        )}

        {blocker && (
          <p role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">
            {blocker}
          </p>
        )}

        {!blocker && busyCount > 0 && (
          <p role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">
            This playbook still has {busyCount} unfinished run or pending event. The daemon
            refuses to delete an entry that owns unfinished work.
          </p>
        )}

        {fatal && (
          <div role="alert" className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
            <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{fatal}</span>
          </div>
        )}

        <div className="flex items-center justify-end gap-2 border-t border-gray-800 pt-3">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={!!blocker || del.isPending}
            className="rounded-md bg-red-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:cursor-not-allowed disabled:bg-gray-700"
          >
            {del.isPending ? "Deleting..." : "Delete playbook"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
