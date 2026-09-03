import type { EditProfileRequest } from "../../api/client";
import type { ProfileDetail } from "../../api/hooks";

export interface ProfileFormState {
  name: string;
  description: string;
  harness: string;
  default_class: string;
  permission_mode: string;
  codex_full_auto: boolean;
  claude_dangerously_skip_permissions: boolean;
  system_prompt_suffix: string;
  allowed_tools: string[];
  mcp_servers: string[];
}

export function profileToForm(p: ProfileDetail | null | undefined): ProfileFormState {
  const dc = (p as { default_class?: string } | null | undefined)?.default_class;
  const rawPerm = p?.permission_mode ?? "";
  const legacyClaudeBypass =
    p?.harness === "claude" && rawPerm === "bypassPermissions";
  return {
    name: p?.name ?? "",
    description: p?.description ?? "",
    harness: p?.harness ?? "",
    default_class: dc ?? "",
    permission_mode:
      rawPerm === "(default)" || legacyClaudeBypass ? "" : rawPerm,
    codex_full_auto: p?.codex_full_auto ?? false,
    claude_dangerously_skip_permissions:
      (p?.claude_dangerously_skip_permissions ?? false) || legacyClaudeBypass,
    system_prompt_suffix: p?.system_prompt_suffix ?? "",
    allowed_tools: [...(p?.allowed_tools ?? [])],
    mcp_servers: [...(p?.mcp_servers ?? [])],
  };
}

export type ProfileEditPayload = EditProfileRequest;

/** Body the system (global) profile drawer sends to `edit_profile`. */
export function profileEditPayload(
  profileId: string,
  form: ProfileFormState,
): ProfileEditPayload {
  return {
    profile_id: profileId,
    ...commonEditFields(form),
  };
}

// An empty text box means "clear this field", and mcp_servers is a list of
// registry names — an empty list is a legal value that clears every server,
// not an omission.
function commonEditFields(form: ProfileFormState) {
  return {
    name: form.name || null,
    description: form.description || null,
    default_class: form.default_class || "",
    permission_mode: form.permission_mode || null,
    codex_full_auto: form.codex_full_auto,
    claude_dangerously_skip_permissions:
      form.claude_dangerously_skip_permissions,
    system_prompt_suffix: form.system_prompt_suffix || null,
    allowed_tools: form.allowed_tools,
    mcp_servers: form.mcp_servers,
  };
}
