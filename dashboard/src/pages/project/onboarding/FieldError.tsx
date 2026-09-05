import { currentFieldError, fieldErrorId, useWizard } from "./context";

/** Inline error text for a field; renders nothing while the field is valid. */
export function FieldError({ name }: { name: string }) {
  const { state } = useWizard();
  const error = currentFieldError(state, name);
  if (!error) return null;
  return (
    <p id={fieldErrorId(name)} className="mt-1 flex items-start gap-1 text-sm text-red-300">
      <span aria-hidden="true">⚠</span>
      <span>{error}</span>
    </p>
  );
}
