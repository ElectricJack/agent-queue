import { useEffect, useMemo, useState } from "react";
import {
  ArrowPathIcon,
  ExclamationTriangleIcon,
  EyeIcon,
} from "@heroicons/react/24/outline";

import {
  useSystemConfig,
  useUpdateSystemConfig,
} from "../../api/hooks";
import { useDigestPreview, useDigestStatus } from "../../api/messaging";
import EscalationInbox from "./EscalationInbox";

/** Mirrors src.digest.facts.CATEGORIES; the daemon rejects anything else. */
const CATEGORIES = ["work", "vcs", "budget", "system"] as const;

/** §9 implementation defaults. The daemon validates these bounds again. */
const INTERVAL_MIN = 15;
const INTERVAL_MAX = 1440;
const CATCHUP_MIN = 1;
const CATCHUP_MAX = 168;

interface DigestSection {
  enabled: boolean;
  interval_minutes: number;
  project_ids: string[];
  categories: string[];
  catchup_hours: number;
}

interface EscalationSection {
  enabled: boolean;
  mention_user_ids: string[];
  mention_role_ids: string[];
  reminder_minutes: number;
  supervisor_delivery_timeout_minutes: number;
}

interface DiscordSection {
  channel_id: string;
  digest: DigestSection;
  escalation: EscalationSection;
  [key: string]: unknown;
}

const SNOWFLAKE = /^\d{17,20}$/;

function asStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function asSection(raw: unknown): DiscordSection {
  const source = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const digest = (source.digest && typeof source.digest === "object" ? source.digest : {}) as Record<string, unknown>;
  const escalation = (source.escalation && typeof source.escalation === "object" ? source.escalation : {}) as Record<string, unknown>;
  return {
    ...source,
    channel_id: typeof source.channel_id === "string" ? source.channel_id : "",
    digest: {
      enabled: digest.enabled !== false,
      interval_minutes: typeof digest.interval_minutes === "number" ? digest.interval_minutes : 60,
      project_ids: asStringList(digest.project_ids),
      categories: digest.categories === undefined ? [...CATEGORIES] : asStringList(digest.categories),
      catchup_hours: typeof digest.catchup_hours === "number" ? digest.catchup_hours : 24,
    },
    escalation: {
      enabled: escalation.enabled !== false,
      mention_user_ids: asStringList(escalation.mention_user_ids),
      mention_role_ids: asStringList(escalation.mention_role_ids),
      reminder_minutes: typeof escalation.reminder_minutes === "number" ? escalation.reminder_minutes : 0,
      supervisor_delivery_timeout_minutes:
        typeof escalation.supervisor_delivery_timeout_minutes === "number"
          ? escalation.supervisor_delivery_timeout_minutes
          : 15,
    },
  };
}

/** Client-side echo of the daemon's bounds so a bad value never round-trips. */
function validate(section: DiscordSection): string[] {
  const errors: string[] = [];
  const needsChannel = section.digest.enabled || section.escalation.enabled;
  if (section.channel_id && !SNOWFLAKE.test(section.channel_id)) {
    errors.push("Channel ID must be a Discord ID (17-20 digits), not a channel name.");
  }
  if (!section.channel_id && needsChannel) {
    errors.push("A channel ID is required while digests or escalation posting are enabled.");
  }
  if (section.digest.interval_minutes < INTERVAL_MIN || section.digest.interval_minutes > INTERVAL_MAX) {
    errors.push(`Digest interval must be between ${INTERVAL_MIN} and ${INTERVAL_MAX} minutes.`);
  }
  if (section.digest.catchup_hours < CATCHUP_MIN || section.digest.catchup_hours > CATCHUP_MAX) {
    errors.push(`Catch-up horizon must be between ${CATCHUP_MIN} and ${CATCHUP_MAX} hours.`);
  }
  if (section.digest.categories.length === 0) {
    errors.push("Select at least one digest category, or the digest can never send.");
  }
  for (const id of [...section.escalation.mention_user_ids, ...section.escalation.mention_role_ids]) {
    if (!SNOWFLAKE.test(id)) errors.push(`Mention ID ${id} is not a Discord ID (17-20 digits).`);
  }
  const reminder = section.escalation.reminder_minutes;
  if (reminder !== 0 && (reminder < 5 || reminder > 1440)) {
    errors.push("Reminder minutes must be 0 (disabled) or between 5 and 1440.");
  }
  const timeout = section.escalation.supervisor_delivery_timeout_minutes;
  if (timeout < 1 || timeout > 1440) {
    errors.push("Supervisor delivery timeout must be between 1 and 1440 minutes.");
  }
  return errors;
}

function formatTime(epoch: number | null | undefined): string {
  if (!epoch) return "—";
  return new Date(epoch * 1000).toLocaleString();
}

function idList(value: string): string[] {
  return value.split(/[\s,]+/).map((v) => v.trim()).filter(Boolean);
}

export default function Messaging() {
  const { data: configData, isLoading, error } = useSystemConfig();
  const update = useUpdateSystemConfig();
  const status = useDigestStatus();
  const [previewRequested, setPreviewRequested] = useState(false);
  const preview = useDigestPreview(previewRequested);

  const [draft, setDraft] = useState<DiscordSection | null>(null);
  const [serverErrors, setServerErrors] = useState<string[]>([]);
  const [saved, setSaved] = useState(false);

  const loaded = useMemo(
    () => (configData ? asSection((configData.config ?? {}).discord) : null),
    [configData],
  );

  useEffect(() => {
    if (loaded && draft === null) setDraft(loaded);
  }, [loaded, draft]);

  if (error) {
    return (
      <div className="rounded-lg border border-red-900/40 bg-red-950/30 p-4 text-sm text-red-200">
        Failed to load config: {(error as Error).message}
      </div>
    );
  }
  if (isLoading || !draft) return <div className="text-sm text-gray-400">Loading messaging settings…</div>;

  const localErrors = validate(draft);
  const digest = draft.digest;
  const escalation = draft.escalation;

  const patch = (next: Partial<DiscordSection>) => {
    setSaved(false);
    setDraft({ ...draft, ...next });
  };
  const patchDigest = (next: Partial<DigestSection>) => patch({ digest: { ...digest, ...next } });
  const patchEscalation = (next: Partial<EscalationSection>) =>
    patch({ escalation: { ...escalation, ...next } });

  const save = async () => {
    setServerErrors([]);
    setSaved(false);
    try {
      const result = await update.mutateAsync({ section: "discord", data: draft });
      const validation = (result as { validation_errors?: string[] })?.validation_errors ?? [];
      if (validation.length > 0) {
        setServerErrors(validation);
        return;
      }
      setSaved(true);
      await status.refetch();
    } catch (e) {
      setServerErrors([(e as Error).message]);
    }
  };

  const statusData = status.data;
  const health = statusData?.delivery_health ?? {};
  const attention = Object.entries(health).filter(([, count]) => (count ?? 0) > 0);

  return (
    <div className="space-y-6">
      <header>
        <h2 className="text-xl font-bold">Messaging</h2>
        <p className="text-sm text-gray-500">
          One shared Discord channel: an hourly activity digest and immediate escalation threads.
          Replies always go to the owning project supervisor.
        </p>
      </header>

      {!escalation.enabled && (
        <div
          role="alert"
          className="flex gap-2 rounded-lg border border-amber-900/40 bg-amber-950/30 p-3 text-sm text-amber-200"
        >
          <ExclamationTriangleIcon className="h-5 w-5 shrink-0" />
          <span>
            External escalation posting is disabled. Escalations are still created, the owning
            supervisor is still notified and this dashboard's escalation inbox still works —
            nothing is posted to Discord.
          </span>
        </div>
      )}

      <section className="space-y-4 rounded-lg border border-gray-800 p-4">
        <h3 className="font-semibold">Destination</h3>
        <label className="block text-sm" htmlFor="discord-channel-id">
          <span className="text-gray-400">Channel ID</span>
          <input
            id="discord-channel-id"
            className="mt-1 w-full rounded-md border border-gray-700 bg-gray-900 px-2 py-1 font-mono"
            value={draft.channel_id}
            onChange={(e) => patch({ channel_id: e.target.value.trim() })}
          />
        </label>
        <p className="text-xs text-gray-500">
          The one configured channel, addressed by ID so a rename never orphans pending deliveries.
        </p>
      </section>

      <section className="space-y-4 rounded-lg border border-gray-800 p-4">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold">Hourly digest</h3>
          <label className="flex items-center gap-2 text-sm" htmlFor="digest-enabled">
            <input
              id="digest-enabled"
              type="checkbox"
              checked={digest.enabled}
              onChange={(e) => patchDigest({ enabled: e.target.checked })}
            />
            <span>Enabled</span>
          </label>
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <label className="block text-sm" htmlFor="digest-interval">
            <span className="text-gray-400">Interval (minutes)</span>
            <input
              id="digest-interval"
              type="number"
              min={INTERVAL_MIN}
              max={INTERVAL_MAX}
              className="mt-1 w-full rounded-md border border-gray-700 bg-gray-900 px-2 py-1"
              value={digest.interval_minutes}
              onChange={(e) => patchDigest({ interval_minutes: Number(e.target.value) })}
            />
          </label>
          <label className="block text-sm" htmlFor="digest-catchup">
            <span className="text-gray-400">Catch-up horizon (hours)</span>
            <input
              id="digest-catchup"
              type="number"
              min={CATCHUP_MIN}
              max={CATCHUP_MAX}
              className="mt-1 w-full rounded-md border border-gray-700 bg-gray-900 px-2 py-1"
              value={digest.catchup_hours}
              onChange={(e) => patchDigest({ catchup_hours: Number(e.target.value) })}
            />
          </label>
        </div>
        <fieldset className="space-y-2">
          <legend className="text-sm text-gray-400">Categories</legend>
          <div className="flex flex-wrap gap-3">
            {CATEGORIES.map((category) => (
              <label key={category} className="flex items-center gap-2 text-sm" htmlFor={`digest-category-${category}`}>
                <input
                  id={`digest-category-${category}`}
                  type="checkbox"
                  checked={digest.categories.includes(category)}
                  onChange={(e) =>
                    patchDigest({
                      categories: e.target.checked
                        ? [...digest.categories, category]
                        : digest.categories.filter((c) => c !== category),
                    })
                  }
                />
                <span>{category}</span>
              </label>
            ))}
          </div>
        </fieldset>
        <label className="block text-sm" htmlFor="digest-projects">
          <span className="text-gray-400">Projects (blank = every project this destination may see)</span>
          <input
            id="digest-projects"
            className="mt-1 w-full rounded-md border border-gray-700 bg-gray-900 px-2 py-1 font-mono"
            value={digest.project_ids.join(", ")}
            onChange={(e) => patchDigest({ project_ids: idList(e.target.value) })}
          />
        </label>
      </section>

      <section className="space-y-4 rounded-lg border border-gray-800 p-4">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold">Escalation threads</h3>
          <label className="flex items-center gap-2 text-sm" htmlFor="escalation-enabled">
            <input
              id="escalation-enabled"
              type="checkbox"
              checked={escalation.enabled}
              onChange={(e) => patchEscalation({ enabled: e.target.checked })}
            />
            <span>Post to Discord</span>
          </label>
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <label className="block text-sm" htmlFor="escalation-users">
            <span className="text-gray-400">Mention user IDs</span>
            <input
              id="escalation-users"
              className="mt-1 w-full rounded-md border border-gray-700 bg-gray-900 px-2 py-1 font-mono"
              value={escalation.mention_user_ids.join(", ")}
              onChange={(e) => patchEscalation({ mention_user_ids: idList(e.target.value) })}
            />
          </label>
          <label className="block text-sm" htmlFor="escalation-roles">
            <span className="text-gray-400">Mention role IDs</span>
            <input
              id="escalation-roles"
              className="mt-1 w-full rounded-md border border-gray-700 bg-gray-900 px-2 py-1 font-mono"
              value={escalation.mention_role_ids.join(", ")}
              onChange={(e) => patchEscalation({ mention_role_ids: idList(e.target.value) })}
            />
          </label>
          <label className="block text-sm" htmlFor="escalation-reminder">
            <span className="text-gray-400">Reminder (minutes, 0 = off)</span>
            <input
              id="escalation-reminder"
              type="number"
              min={0}
              max={1440}
              className="mt-1 w-full rounded-md border border-gray-700 bg-gray-900 px-2 py-1"
              value={escalation.reminder_minutes}
              onChange={(e) => patchEscalation({ reminder_minutes: Number(e.target.value) })}
            />
          </label>
          <label className="block text-sm" htmlFor="escalation-timeout">
            <span className="text-gray-400">Supervisor delivery timeout (minutes)</span>
            <input
              id="escalation-timeout"
              type="number"
              min={1}
              max={1440}
              className="mt-1 w-full rounded-md border border-gray-700 bg-gray-900 px-2 py-1"
              value={escalation.supervisor_delivery_timeout_minutes}
              onChange={(e) =>
                patchEscalation({ supervisor_delivery_timeout_minutes: Number(e.target.value) })
              }
            />
          </label>
        </div>
        <p className="text-xs text-gray-500">
          Digests never mention anyone; mentions are only used on the escalation root post.
        </p>
      </section>

      {(localErrors.length > 0 || serverErrors.length > 0) && (
        <ul role="alert" className="space-y-1 rounded-lg border border-red-900/40 bg-red-950/30 p-3 text-sm text-red-200">
          {[...localErrors, ...serverErrors].map((message) => (
            <li key={message}>{message}</li>
          ))}
        </ul>
      )}
      {saved && <p className="text-sm text-emerald-400">Messaging settings saved.</p>}

      <div className="flex items-center gap-3">
        <button
          type="button"
          className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          disabled={localErrors.length > 0 || update.isPending}
          onClick={save}
        >
          Save messaging settings
        </button>
        <button
          type="button"
          className="flex items-center gap-1 rounded-md border border-gray-700 px-3 py-1.5 text-sm"
          onClick={() => {
            setPreviewRequested(true);
            void preview.refetch();
          }}
        >
          <EyeIcon className="h-4 w-4" />
          Preview digest
        </button>
        <button
          type="button"
          className="flex items-center gap-1 rounded-md border border-gray-700 px-3 py-1.5 text-sm"
          onClick={() => void status.refetch()}
        >
          <ArrowPathIcon className="h-4 w-4" />
          Refresh status
        </button>
      </div>

      <section className="space-y-2 rounded-lg border border-gray-800 p-4">
        <h3 className="font-semibold">Schedule and delivery health</h3>
        {statusData ? (
          <dl className="grid gap-x-6 gap-y-1 text-sm md:grid-cols-2">
            <div className="flex justify-between gap-4"><dt className="text-gray-400">Destination</dt><dd className="font-mono">{statusData.destination}</dd></div>
            <div className="flex justify-between gap-4"><dt className="text-gray-400">Config generation</dt><dd className="font-mono">{statusData.config_generation}</dd></div>
            <div className="flex justify-between gap-4"><dt className="text-gray-400">Next evaluation</dt><dd>{formatTime(statusData.next_evaluation_at)}</dd></div>
            <div className="flex justify-between gap-4"><dt className="text-gray-400">Last window ended</dt><dd>{formatTime(statusData.last_window_end)}</dd></div>
            <div className="flex justify-between gap-4"><dt className="text-gray-400">Open escalations</dt><dd>{statusData.open_escalations}</dd></div>
            <div className="flex justify-between gap-4"><dt className="text-gray-400">Pending escalation deliveries</dt><dd>{statusData.pending_escalation_deliveries}</dd></div>
          </dl>
        ) : (
          <p className="text-sm text-gray-400">Loading schedule status…</p>
        )}
        <p className="text-sm">
          {attention.length === 0
            ? "No digest delivery needs attention."
            : `Digest deliveries needing attention: ${attention.map(([k, v]) => `${k} ${v}`).join(", ")}`}
        </p>
        {(statusData?.settings_errors ?? []).length > 0 && (
          <ul role="alert" className="space-y-1 text-sm text-red-300">
            {statusData?.settings_errors?.map((message) => <li key={message}>{message}</li>)}
          </ul>
        )}
      </section>

      {previewRequested && (
        <section className="space-y-2 rounded-lg border border-gray-800 p-4">
          <h3 className="font-semibold">Digest preview</h3>
          <p className="text-xs text-gray-500">
            A dry evaluation: nothing is sent and no delivery cursor moves.
          </p>
          {preview.isLoading && <p className="text-sm text-gray-400">Evaluating…</p>}
          {preview.error && (
            <p role="alert" className="text-sm text-red-300">{(preview.error as Error).message}</p>
          )}
          {preview.data && (
            preview.data.would_send ? (
              <pre className="overflow-x-auto rounded-md bg-gray-900 p-3 text-sm whitespace-pre-wrap">{preview.data.text}</pre>
            ) : (
              <p className="text-sm text-gray-300">
                Nothing would be sent — suppression reason:{" "}
                <span className="font-mono">{preview.data.suppression_reason ?? preview.data.reason}</span>
              </p>
            )
          )}
        </section>
      )}

      <EscalationInbox />
    </div>
  );
}
