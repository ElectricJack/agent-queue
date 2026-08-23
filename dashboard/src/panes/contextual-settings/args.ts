import { z } from "zod";

const projectArgs = z.object({
  subject: z.literal("project"),
  subjectId: z.string().min(1), // project id
});
const profileArgs = z.object({
  subject: z.literal("profile"),
  subjectId: z.string().min(1), // system profile id (agent_type)
});
const projectProfileArgs = z.object({
  subject: z.literal("project-profile"),
  subjectId: z.string().min(1), // agent_type
  projectId: z.string().min(1),
});
const playbookArgs = z.object({
  subject: z.literal("playbook"),
  subjectId: z.string().min(1), // playbook id
});
const intelligenceClassArgs = z.object({
  subject: z.literal("intelligence-class"),
  subjectId: z.string().min(1), // class id
});

export const contextualSettingsArgsSchema = z.discriminatedUnion("subject", [
  projectArgs,
  profileArgs,
  projectProfileArgs,
  playbookArgs,
  intelligenceClassArgs,
]);
export type ContextualSettingsArgs = z.infer<typeof contextualSettingsArgsSchema>;
