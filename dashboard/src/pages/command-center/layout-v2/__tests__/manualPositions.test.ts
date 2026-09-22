import { describe, expect, it } from "vitest";
import { PLAYBOOK_POSITION_SCOPE, type ManualPosition, type ManualPositions } from "../manualPositions";

describe("manual graph position shapes", () => {
  it("reserves the persisted position map for the separate playbook graph", () => {
    const position: ManualPosition = { x: 1.2, y: 3.4 };
    const positions: ManualPositions = { [PLAYBOOK_POSITION_SCOPE]: { "playbook:x": position } };
    expect(positions[PLAYBOOK_POSITION_SCOPE]?.["playbook:x"]).toEqual(position);
  });
});
