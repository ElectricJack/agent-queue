import type { PaneViewProps } from "../types";

export default function StubSmoke({ args }: PaneViewProps<{ text: string }>) {
  return <div data-testid="stub-smoke">{args.text}</div>;
}
