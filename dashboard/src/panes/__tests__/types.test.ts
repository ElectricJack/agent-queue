import { z } from "zod";
import type {
  PaneManifest,
  PaneViewProps,
  PaneToolbarAction,
  ShortcutBinding,
  HeroIcon,
} from "../types";

// Compile-time check only — runtime test is trivial.
test("PaneManifest accepts a well-typed args schema", () => {
  const argsSchema = z.object({ taskId: z.string() });
  type Args = z.infer<typeof argsSchema>;
  const manifest: PaneManifest<Args> = {
    id: "task-detail",
    name: "Task detail",
    description: "Detail view for a task",
    icon: (() => null) as HeroIcon,
    args_schema: argsSchema,
  };
  expect(manifest.id).toBe("task-detail");
});

test("PaneViewProps carries typed args + shell hooks", () => {
  type Args = { taskId: string };
  const propsShape: PaneViewProps<Args> = {
    args: { taskId: "t1" },
    close: () => {},
    setArgs: (_next: Args) => {},
    setToolbar: (_a: PaneToolbarAction[]) => {},
    setShortcuts: (_b: ShortcutBinding[]) => {},
  };
  expect(propsShape.args.taskId).toBe("t1");
});
