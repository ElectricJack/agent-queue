import { useEffect, useRef } from "react";
import type { PaneViewProps, PaneToolbarAction } from "../types";
import type { ContextualSettingsArgs } from "./args";
import ProjectSubject from "./subjects/ProjectSubject";
import ProfileSubject from "./subjects/ProfileSubject";
import PlaybookSubject from "./subjects/PlaybookSubject";
import IntelligenceClassSubject from "./subjects/IntelligenceClassSubject";

export default function ContextualSettingsPane(props: PaneViewProps<ContextualSettingsArgs>) {
  const { args, close, setToolbar, setShortcuts } = props;
  const toolbarRef = useRef<PaneToolbarAction[]>([]);

  const wrappedSetToolbar = (actions: PaneToolbarAction[]) => {
    toolbarRef.current = actions;
    setToolbar(actions);
  };

  useEffect(() => {
    const isDirty = () => {
      const discard = toolbarRef.current.find((a) => a.id === "discard");
      return discard ? !discard.disabled : false;
    };
    const save = () => {
      const saveAction = toolbarRef.current.find((a) => a.id === "save");
      if (saveAction && !saveAction.disabled) saveAction.onClick();
    };
    const handleEscape = () => {
      if (!isDirty()) {
        close();
        return;
      }
      if (window.confirm("Discard unsaved changes to this settings pane?")) close();
    };

    if (args.subject === "intelligence-class") {
      setShortcuts([]);
      return;
    }
    setShortcuts([
      { key: "$mod-s", label: "Save", onFire: save },
      { key: "Escape", label: "Discard & close", onFire: handleEscape },
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [args.subject, close]);

  switch (args.subject) {
    case "project":
      return <ProjectSubject {...props} args={args} setToolbar={wrappedSetToolbar} />;
    case "profile":
      return <ProfileSubject {...props} args={args} setToolbar={wrappedSetToolbar} />;
    case "playbook":
      return <PlaybookSubject {...props} args={args} setToolbar={wrappedSetToolbar} />;
    case "intelligence-class":
      return <IntelligenceClassSubject {...props} args={args} setToolbar={wrappedSetToolbar} />;
    default: {
      const _exhaustive: never = args;
      return _exhaustive;
    }
  }
}
