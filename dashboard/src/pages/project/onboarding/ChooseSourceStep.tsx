import { SOURCE_MODES } from "./state";
import { useStepIds, useWizard } from "./context";
import { SOURCE_MODE_COPY } from "./copy";

export function ChooseSourceStep() {
  const { state, dispatch } = useWizard();
  const id = useStepIds("source");
  return (
    <div className="space-y-3">
      <p id={id("legend")} className="text-sm text-gray-400">Source</p>
      <div role="radiogroup" aria-labelledby={id("legend")} className="space-y-2">
        {SOURCE_MODES.map((mode) => {
          const copy = SOURCE_MODE_COPY[mode];
          const checked = state.source.mode === mode;
          return (
            <label
              key={mode}
              className={`flex cursor-pointer items-start gap-3 rounded-lg border px-3 py-2 ${
                checked ? "border-indigo-400 bg-indigo-500/10" : "border-gray-700 hover:border-gray-500"
              }`}
            >
              <input
                type="radio"
                name={id("mode")}
                value={mode}
                checked={checked}
                onChange={() => dispatch({ type: "set_source_mode", mode })}
                className="mt-1 h-4 w-4 accent-indigo-500"
              />
              <span className="flex flex-col">
                <span className="text-sm font-medium text-gray-100">{copy.label}</span>
                <span className="text-xs text-gray-400">{copy.description}</span>
              </span>
            </label>
          );
        })}
      </div>
    </div>
  );
}
