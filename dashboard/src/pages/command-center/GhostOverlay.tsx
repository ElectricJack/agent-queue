import { useQuery } from "@tanstack/react-query";
import { legacyFetch } from "../../api/legacy-fetch";

interface ProposalResponse {
  proposal_id: string;
  tasks: Array<{ id: string; title: string }>;
  edges: Array<{ from: string; to: string; dep_type: string }>;
  status: string;
}

interface Props {
  proposalId: string | null;
}

/** Ghost preview of a Phase 6 task-batch proposal.  Feature-detects the
 *  endpoint — if the server returns 404 (Phase 6 not deployed), renders
 *  nothing and stays silent. */
export default function GhostOverlay({ proposalId }: Props) {
  const { data } = useQuery<ProposalResponse | null>({
    queryKey: ["proposal", proposalId],
    enabled: !!proposalId,
    retry: false,
    queryFn: async () => {
      if (!proposalId) return null;
      // Phase 6 hasn't landed the endpoint yet, so the generated SDK doesn't
      // include it — use legacyFetch (respects VITE_API_URL) instead of a
      // bare fetch. Swap to the generated call when Phase 6 ships and
      // regenerates the client.
      const r = await legacyFetch(`/api/proposals/${proposalId}`);
      if (r.status === 404) {
        console.debug("[GhostOverlay] Phase 6 proposal endpoint 404 — no-op.");
        return null;
      }
      if (!r.ok) throw new Error(`proposal fetch ${r.status}`);
      return (await r.json()) as ProposalResponse;
    },
  });

  if (!data) return null;
  return (
    <div className="pointer-events-none absolute inset-0 z-20">
      {data.tasks.map((t, i) => (
        <div
          key={t.id}
          className="absolute rounded border-2 border-dashed border-fuchsia-400/60 bg-fuchsia-500/5 p-1 text-[10px] text-fuchsia-200"
          style={{ left: 40 + i * 240, top: 40, width: 220 }}
        >
          {t.title}
        </div>
      ))}
    </div>
  );
}
