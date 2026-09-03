import graphFixture from "./graph.fixture.json";
import type { GraphNodeDTO, PlaybookV2GraphResponse } from "../../../api/client";

/** The §10.1 `review-pipeline` artifact exactly as `project_graph` renders it.
 *
 *  `graph.fixture.json` is written by `python -m tests.playbook_v2_helpers` and
 *  asserted byte for byte against a live projection by
 *  `tests/test_playbook_v2_api_dtos.py`, so a component test that reads it is
 *  testing the payload the daemon actually serves. `fixtures.ts` next door now
 *  reads its nodes from here too (plan §16.12): a hand-authored node can agree
 *  with a component while disagreeing with the projector, which is how the
 *  compact card came to omit its key inputs and its output binding for a whole
 *  package (`solid-harbor.49` pass 2).
 *
 *  The cast is the one place the JSON meets the generated types: the fixture is
 *  a JSON module, so TypeScript widens its literal unions to `string`. */
export const projectedGraph = graphFixture as unknown as PlaybookV2GraphResponse;

/** One projected node by artifact-local step id. Throws rather than returning
 *  `undefined`, so a renamed step fails the test that names it. */
export function projectedNode(stepId: string): GraphNodeDTO {
  const node = projectedGraph.nodes?.find((candidate) => candidate.id === stepId);
  if (!node) throw new Error(`no projected node ${stepId} in graph.fixture.json`);
  return node;
}
