import { useState } from "react";

export function useDirtyForm<T>(initial: T) {
  const [value, setValue] = useState<T>(initial);
  const [baseline, setBaseline] = useState<T>(initial);
  const dirty = JSON.stringify(value) !== JSON.stringify(baseline);
  const resetBaseline = (next: T) => {
    setValue(next);
    setBaseline(next);
  };
  return { value, setValue, dirty, resetBaseline };
}
