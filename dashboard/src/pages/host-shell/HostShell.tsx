import { useEffect, useState } from "react";
import { CommandLineIcon, PlusIcon, TrashIcon } from "@heroicons/react/24/outline";
import InteractiveTerminal from "../../components/InteractiveTerminal";
import { useCloseHostShell, useHostShells, useOpenHostShell } from "../../api/hostShell";

const BUTTON = "inline-flex items-center gap-1 rounded px-2 py-1 text-xs text-gray-200 hover:bg-gray-800 focus-visible:outline-2 focus-visible:outline-indigo-400 disabled:opacity-40";

/**
 * Host shell: an interactive login shell on the machine running AQ, for
 * remote management. Not an agent: no task, no session row. Each shell is a
 * tmux session, so it survives reloads and is reattached from this list.
 */
export default function HostShell() {
  const shells = useHostShells();
  const open = useOpenHostShell();
  const close = useCloseHostShell();
  const [selected, setSelected] = useState<string | null>(null);
  const list = shells.data?.shells ?? [];

  useEffect(() => {
    if (selected && !list.some((shell) => shell.name === selected)) setSelected(null);
    const firstShell = list[0];
    if (!selected && firstShell) setSelected(firstShell.name);
  }, [list, selected]);

  if (shells.isError) {
    return <div role="alert" className="p-4 text-sm text-amber-300">
      Could not reach host shells. {String((shells.error as Error)?.message ?? "")}
    </div>;
  }
  if (shells.data && !shells.data.enabled) {
    return <div className="p-4 text-sm text-gray-400">
      Host shells are off. Set <code>dashboard.host_shell.enabled: true</code> in the daemon config to allow
      opening an operator shell on this machine.
    </div>;
  }
  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex flex-wrap items-center gap-2 border-b border-gray-800 px-3 py-2">
        <CommandLineIcon aria-hidden="true" className="h-4 w-4 text-gray-400" />
        <h1 className="text-sm font-semibold text-gray-100">Host shell</h1>
        <div role="tablist" aria-label="Open host shells" className="flex flex-wrap gap-1">
          {list.map((shell) => (
            <button key={shell.name} role="tab" type="button" aria-selected={shell.name === selected}
              onClick={() => setSelected(shell.name)}
              className={BUTTON + (shell.name === selected ? " bg-gray-800" : "")}>{shell.name}</button>
          ))}
        </div>
        <button type="button" className={BUTTON} disabled={open.isPending}
          onClick={() => open.mutate(undefined, { onSuccess: (data) => setSelected(data.shell.name) })}>
          <PlusIcon aria-hidden="true" className="h-4 w-4" />Open host shell
        </button>
        {selected && <button type="button" className={BUTTON} disabled={close.isPending}
          aria-label={"Close " + selected} onClick={() => close.mutate(selected)}>
          <TrashIcon aria-hidden="true" className="h-4 w-4" />Close shell
        </button>}
        {(open.error || close.error) && <span role="alert" className="text-xs text-amber-300">
          {String(((open.error || close.error) as Error).message)}
        </span>}
      </header>
      <div className="min-h-0 flex-1">
        {selected
          ? <InteractiveTerminal key={selected} sessionId={selected} name={selected} />
          : <p className="p-4 text-sm text-gray-400">No host shell is open. Open one to get a login shell in $HOME.</p>}
      </div>
    </div>
  );
}
