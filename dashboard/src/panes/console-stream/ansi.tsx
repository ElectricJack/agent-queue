// Small local ANSI SGR converter for console output — not a full terminal
// emulator. Handles reset (0), bold (1), and the 16 standard/bright colors
// (30-37, 90-97). No new dependency (spec §5.1).

import type { ReactNode } from "react";

const SGR_COLORS: Record<number, string> = {
  30: "#000000", 31: "#e06c75", 32: "#98c379", 33: "#e5c07b",
  34: "#61afef", 35: "#c678dd", 36: "#56b6c2", 37: "#dcdfe4",
  90: "#5c6370", 91: "#e06c75", 92: "#98c379", 93: "#e5c07b",
  94: "#61afef", 95: "#c678dd", 96: "#56b6c2", 97: "#ffffff",
};

// eslint-disable-next-line no-control-regex
const SGR_RE = /\x1b\[([0-9;]*)m/g;

export function ansiToSpans(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let color: string | undefined;
  let bold = false;
  let lastIndex = 0;
  let key = 0;

  SGR_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = SGR_RE.exec(text)) !== null) {
    const chunk = text.slice(lastIndex, match.index);
    if (chunk) {
      nodes.push(
        <span key={key++} style={{ color, fontWeight: bold ? "bold" : undefined }}>
          {chunk}
        </span>,
      );
    }
    const codes = match[1].split(";").filter(Boolean).map(Number);
    if (codes.length === 0) codes.push(0);
    for (const code of codes) {
      if (code === 0) {
        color = undefined;
        bold = false;
      } else if (code === 1) {
        bold = true;
      } else if (SGR_COLORS[code]) {
        color = SGR_COLORS[code];
      }
    }
    lastIndex = SGR_RE.lastIndex;
  }

  const rest = text.slice(lastIndex);
  if (rest) {
    nodes.push(
      <span key={key++} style={{ color, fontWeight: bold ? "bold" : undefined }}>
        {rest}
      </span>,
    );
  }
  return nodes;
}

export function stripAnsi(text: string): string {
  return text.replace(SGR_RE, "");
}
