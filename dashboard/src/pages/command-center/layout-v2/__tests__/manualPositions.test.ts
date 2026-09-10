import { describe, expect, it } from "vitest";
import { PLAYBOOK_POSITION_SCOPE, type ManualPosition, type ManualPositions } from "../manualPositions";

describe("manual graph position shapes", () => {
  it("keeps project and playbook position maps independently addressable", () => {
    const position: ManualPosition = { x: 1.2, y: 3.4 };
    const positions: ManualPositions = { p1: { "task-1": position }, [PLAYBOOK_POSITION_SCOPE]: { "playbook:x": position } };
    expect(positions.p1?.["task-1"]).toEqual(position);
    expect(positions[PLAYBOOK_POSITION_SCOPE]?.["playbook:x"]).toEqual(position);
  });
});
