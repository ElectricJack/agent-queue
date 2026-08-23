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
