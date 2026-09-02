import { z } from "zod";
import type {
  PaneManifest,
  PaneViewProps,
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
    setArgs: () => {},
    setToolbar: () => {},
    setShortcuts: () => {},
  };
  expect(propsShape.args.taskId).toBe("t1");
});
