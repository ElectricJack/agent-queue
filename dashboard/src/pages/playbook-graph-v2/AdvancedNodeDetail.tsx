import type { GraphNodeDTO } from "../../api/client";
import { Block, Rows } from "./IntentSections";

function Json({ value }: { value: unknown }) {
  return (
    <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded border border-gray-800 bg-gray-950 p-2 font-mono text-[11px] leading-4 text-gray-300">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function Pairs({ pairs }: { pairs: [string, string][] }) {
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
      {pairs.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-gray-500">{label}</dt>
          <dd className="min-w-0 break-words font-mono text-gray-200">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export interface AdvancedNodeDetailProps {
  node: GraphNodeDTO;
}

/** The canonical payload behind a step: the exact typed step from the
 *  artifact, the resolved inputs, the declared result schema, retry and
 *  idempotency, the redaction decisions and the execution fingerprint.
 *
 *  This is a disclosure, never the default view — the explanation is what an
 *  operator reads first, and the JSON is what they fall back to. */
export default function AdvancedNodeDetail({ node }: AdvancedNodeDetailProps) {
  const advanced = node.advanced;
  const resolved = advanced.resolved_inputs ?? [];
  const redaction = advanced.redaction ?? [];

  return (
    <div className="min-w-0 space-y-3" data-testid="advanced-detail">
      <Block name="Identity">
        <Pairs
          pairs={[
            ["step id", node.id],
            ["rule id", node.rule_id],
            ["step kind", node.step_kind],
            ...(node.explanation.contract_fingerprint
              ? ([["contract fingerprint", node.explanation.contract_fingerprint]] as [string, string][])
              : []),
            ...(advanced.execution_fingerprint
              ? ([["execution fingerprint", advanced.execution_fingerprint]] as [string, string][])
              : []),
          ]}
        />
      </Block>

      <Block name="Typed step">
        <Json value={advanced.typed_step} />
      </Block>

      {resolved.length > 0 && (
        <Block name="Resolved inputs">
          <Rows rows={resolved} />
          {resolved.map((row, index) =>
            row.value.redacted || row.value.canonical == null ? null : (
              <Json key={`${row.label}:${index}`} value={row.value.canonical} />
            ),
          )}
        </Block>
      )}

      {advanced.result_schema && (
        <Block name="Result schema">
          <Json value={advanced.result_schema} />
        </Block>
      )}

      {advanced.retry && (
        <Block name="Retry">
          <Pairs
            pairs={[
              ["max attempts", String(advanced.retry.max_attempts ?? 1)],
              ["backoff", advanced.retry.backoff_seconds == null ? "—" : `${advanced.retry.backoff_seconds}s`],
              ["retry on", (advanced.retry.retry_on ?? []).join(", ") || "—"],
            ]}
          />
        </Block>
      )}

      {advanced.idempotency && (
        <Block name="Idempotency">
          <Pairs
            pairs={[
              ["supported", advanced.idempotency.supported ? "yes" : "no"],
              ["key template", advanced.idempotency.key_template ?? "—"],
              [
                "retry safe",
                advanced.idempotency.retry_safe ? "yes" : "no — an ambiguous retry needs an operator decision",
              ],
            ]}
          />
        </Block>
      )}

      {redaction.length > 0 && (
        <Block name="Redaction">
          <table className="w-full text-left text-xs">
            <thead className="text-[10px] uppercase tracking-wide text-gray-500">
              <tr>
                <th className="py-0.5 pr-3 font-medium">Field</th>
                <th className="py-0.5 font-medium">Policy</th>
              </tr>
            </thead>
            <tbody className="text-gray-200">
              {redaction.map((entry) => (
                <tr key={entry.field}>
                  <td className="py-0.5 pr-3 font-mono">{entry.field}</td>
                  <td className="py-0.5">{entry.policy}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Block>
      )}
    </div>
  );
}
