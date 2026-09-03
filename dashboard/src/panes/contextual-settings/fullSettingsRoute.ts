import type { ContextualSettingsArgs } from "./args";

/**
 * Per spec §6.2: none of the editable subjects have a routed detail page
 * keyed by id. Two of four land on a list, not the specific item — see the
 * spec's §13 open question for the v2 follow-up (deep-linking).
 */
export function fullSettingsRoute(args: ContextualSettingsArgs): string {
  switch (args.subject) {
    case "project":
      return `/projects/${args.subjectId}/config`;
    case "profile":
      return "/settings/profiles";
    case "playbook":
      return `/playbooks/${args.subjectId}`;
    case "intelligence-class":
      return "/settings/intelligence-classes";
    default: {
      const _exhaustive: never = args;
      return _exhaustive;
    }
  }
}
