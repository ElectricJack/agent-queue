import { useMutation, useQueryClient } from "@tanstack/react-query";
import { policyApply, policyDiff, policyExport, type ExportRequest, type ImportRequest } from "./client";

export function usePolicyExport() {
  return useMutation({ mutationFn: async (body: ExportRequest) => (await policyExport({ body })).data! });
}

export function usePolicyDiff() {
  return useMutation({ mutationFn: async (body: ImportRequest) => (await policyDiff({ body })).data! });
}

export function usePolicyApply() {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: async (body: ImportRequest) => (await policyApply({ body })).data!,
    onSuccess: () => {
      for (const entity of ["projects", "project", "profiles", "playbooks", "reviews"]) {
        void queries.invalidateQueries({ queryKey: [entity] });
      }
    },
  });
}
