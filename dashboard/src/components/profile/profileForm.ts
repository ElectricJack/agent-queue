import type { EditProfileRequest, EditProjectProfileRequest } from "../../api/client";
import type { ProfileDetail } from "../../api/hooks";

export interface ProfileFormState {
  name: string;
  description: string;
  default_class: string;
  permission_mode: string;
  system_prompt_suffix: string;
  allowed_tools: string[];
  mcp_servers: string[];
}

export function profileToForm(p: ProfileDetail | null | undefined): ProfileFormState {
  const dc = (p as { default_class?: string } | null | undefined)?.default_class;
  const rawPerm = p?.permission_mode ?? "";
  return {
    name: p?.name ?? "",
    description: p?.description ?? "",
    default_class: dc ?? "",
    permission_mode: rawPerm === "(default)" ? "" : rawPerm,
    system_prompt_suffix: p?.system_prompt_suffix ?? "",
    allowed_tools: [...(p?.allowed_tools ?? [])],
    mcp_servers: [...(p?.mcp_servers ?? [])],
  };
}

// `default_class` is accepted by _cmd_edit_profile but is not declared on
// either generated request model, so the API layer drops it before the
// command runs (filed as calm-apex-29).  Spelling it out as an intersection
// keeps the payload honest about what the drawer sends instead of hiding it
// behind an `as unknown as` cast.
export type ProfileEditPayload = EditProfileRequest & { default_class: string };
export type ProjectProfileEditPayload = EditProjectProfileRequest & { default_class: string };

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

/** Body the project profile drawer sends to `edit_project_profile`. */
export function projectProfileEditPayload(
  projectId: string,
  agentType: string,
  form: ProfileFormState,
): ProjectProfileEditPayload {
  return {
    project_id: projectId,
    agent_type: agentType,
    ...commonEditFields(form),
  };
}

// Both edit paths take the same field set: an empty text box means "clear
// this field", and mcp_servers is a list of registry names on both — an
// empty list is a legal value that clears every server, not an omission.
function commonEditFields(form: ProfileFormState) {
  return {
    name: form.name || null,
    description: form.description || null,
    default_class: form.default_class || "",
    permission_mode: form.permission_mode || null,
    system_prompt_suffix: form.system_prompt_suffix || null,
    allowed_tools: form.allowed_tools,
    mcp_servers: form.mcp_servers,
  };
}
