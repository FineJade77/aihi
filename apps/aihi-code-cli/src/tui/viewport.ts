import type { TranscriptEntry } from "../transcript.js";

export type TranscriptLineTone =
  | "normal"
  | "user"
  | "assistant"
  | "muted"
  | "good"
  | "warn"
  | "bad";

export interface TranscriptLine {
  id: string;
  entryId?: string;
  text: string;
  tone: TranscriptLineTone;
  bold?: boolean;
}

export interface TranscriptViewportState {
  /** Exclusive absolute line index. Undefined means follow the live tail. */
  endLine?: number;
  expandedToolDetails: boolean;
}

export interface TranscriptViewport {
  lines: TranscriptLine[];
  startLine: number;
  endLine: number;
  totalLines: number;
  hiddenAbove: number;
  hiddenBelow: number;
  followingTail: boolean;
}

export function createViewportState(): TranscriptViewportState {
  return { expandedToolDetails: false };
}

export function buildTranscriptLines(
  entries: readonly TranscriptEntry[],
  streamText: string,
  columns: number,
  expandedToolDetails: boolean,
): TranscriptLine[] {
  const width = Math.max(12, columns);
  const lines: TranscriptLine[] = [];
  for (const entry of entries) {
    lines.push(...entryLines(entry, width, expandedToolDetails));
  }
  if (streamText) {
    const renderedStream = streamText.length > 4_000
      ? `…${streamText.slice(-4_000)}`
      : streamText;
    lines.push({ id: "stream:label", text: "AIHI · streaming", tone: "assistant", bold: true });
    lines.push(...wrappedLines("stream:text", renderedStream, width, "normal"));
  }
  if (lines.length === 0) {
    lines.push({ id: "empty", text: "No conversation yet", tone: "muted" });
  }
  return lines;
}

export function selectTranscriptViewport(
  lines: readonly TranscriptLine[],
  state: TranscriptViewportState,
  rowBudget: number,
): TranscriptViewport {
  const totalLines = lines.length;
  const budget = Math.max(1, Math.floor(rowBudget));
  const followingTail = state.endLine === undefined || state.endLine >= totalLines;
  const endLine = followingTail
    ? totalLines
    : Math.max(0, Math.min(totalLines, state.endLine ?? totalLines));
  const hiddenBelow = totalLines - endLine;
  let contentRows = Math.max(1, budget - (hiddenBelow > 0 ? 1 : 0));
  let startLine = Math.max(0, endLine - contentRows);
  if (startLine > 0 && budget > 1) {
    contentRows = Math.max(1, contentRows - 1);
    startLine = Math.max(0, endLine - contentRows);
  }
  return {
    lines: lines.slice(startLine, Math.min(endLine, startLine + contentRows)),
    startLine,
    endLine,
    totalLines,
    hiddenAbove: startLine,
    hiddenBelow,
    followingTail,
  };
}

export function scrollTranscriptViewport(
  state: TranscriptViewportState,
  lines: readonly TranscriptLine[],
  rowBudget: number,
  direction: "up" | "down",
): TranscriptViewportState {
  const current = selectTranscriptViewport(lines, state, rowBudget);
  if (direction === "up") {
    if (current.startLine <= 0) return state;
    return { ...state, endLine: current.startLine };
  }
  if (current.hiddenBelow <= 0) return { ...state, endLine: undefined };
  const nextEnd = Math.min(
    current.totalLines,
    current.endLine + Math.max(1, current.lines.length),
  );
  return {
    ...state,
    endLine: nextEnd >= current.totalLines ? undefined : nextEnd,
  };
}

export function followTranscriptTail(state: TranscriptViewportState): TranscriptViewportState {
  return state.endLine === undefined ? state : { ...state, endLine: undefined };
}

export function toggleToolDetails(state: TranscriptViewportState): TranscriptViewportState {
  return { ...state, expandedToolDetails: !state.expandedToolDetails };
}

function entryLines(
  entry: TranscriptEntry,
  width: number,
  expandedToolDetails: boolean,
): TranscriptLine[] {
  if (entry.kind === "user" || entry.kind === "assistant") {
    const tone = entry.kind === "user" ? "user" : "assistant";
    return [
      {
        id: `${entry.id}:label`,
        entryId: entry.id,
        text: entry.kind === "user" ? "YOU" : "AIHI",
        tone,
        bold: true,
      },
      ...wrappedLines(`${entry.id}:text`, entry.text, width, "normal", entry.id),
    ];
  }
  if (entry.kind === "status") {
    const detail = entry.detail ? ` · ${entry.detail}` : "";
    return wrappedLines(
      `${entry.id}:status`,
      `${entry.text}${detail}`,
      width,
      entry.isError ? "bad" : "muted",
      entry.id,
    );
  }
  const tone: TranscriptLineTone = entry.isError
    ? "bad"
    : entry.status === "succeeded"
      ? "good"
      : entry.status === "waiting_approval"
        ? "warn"
        : "muted";
  const result: TranscriptLine[] = [
    {
      id: `${entry.id}:label`,
      entryId: entry.id,
      text: `↳ ${entry.toolName ?? "tool"} · ${entry.status ?? "requested"}`,
      tone,
    },
    ...wrappedLines(`${entry.id}:preview`, `  ${entry.text}`, width, "muted", entry.id),
  ];
  if (entry.detail) {
    if (expandedToolDetails) {
      result.push(...wrappedLines(
        `${entry.id}:detail`,
        `  ${entry.detail}`,
        width,
        entry.isError ? "bad" : "muted",
        entry.id,
      ));
    } else {
      result.push({
        id: `${entry.id}:detail:hidden`,
        entryId: entry.id,
        text: "  … result hidden · Ctrl-O to expand",
        tone: "muted",
      });
    }
  }
  return result;
}

function wrappedLines(
  id: string,
  value: string,
  columns: number,
  tone: TranscriptLineTone,
  entryId?: string,
): TranscriptLine[] {
  const wrapped = wrapTerminalText(value, columns);
  return wrapped.map((text, index) => ({
    id: `${id}:${index}`,
    ...(entryId === undefined ? {} : { entryId }),
    text,
    tone,
  }));
}

/** Small dependency-free terminal wrapper; wide CJK/emoji cells count as two. */
export function wrapTerminalText(value: string, columns: number): string[] {
  const width = Math.max(1, Math.floor(columns));
  const result: string[] = [];
  for (const logicalLine of value.replace(/\t/g, "    ").split("\n")) {
    if (logicalLine.length === 0) {
      result.push("");
      continue;
    }
    let line = "";
    let used = 0;
    for (const character of logicalLine) {
      const cells = terminalCellWidth(character);
      if (used > 0 && used + cells > width) {
        result.push(line);
        line = "";
        used = 0;
      }
      line += character;
      used += cells;
    }
    result.push(line);
  }
  return result.length > 0 ? result : [""];
}

function terminalCellWidth(character: string): number {
  const codePoint = character.codePointAt(0) ?? 0;
  if (codePoint === 0 || codePoint < 32 || (codePoint >= 0x7f && codePoint < 0xa0)) return 0;
  if (/\p{Mark}/u.test(character)) return 0;
  if (
    codePoint >= 0x1100 && (
      codePoint <= 0x115f ||
      codePoint === 0x2329 ||
      codePoint === 0x232a ||
      (codePoint >= 0x2e80 && codePoint <= 0xa4cf && codePoint !== 0x303f) ||
      (codePoint >= 0xac00 && codePoint <= 0xd7a3) ||
      (codePoint >= 0xf900 && codePoint <= 0xfaff) ||
      (codePoint >= 0xfe10 && codePoint <= 0xfe19) ||
      (codePoint >= 0xfe30 && codePoint <= 0xfe6f) ||
      (codePoint >= 0xff00 && codePoint <= 0xff60) ||
      (codePoint >= 0xffe0 && codePoint <= 0xffe6) ||
      (codePoint >= 0x1f300 && codePoint <= 0x1faff) ||
      (codePoint >= 0x20000 && codePoint <= 0x3fffd)
    )
  ) return 2;
  return 1;
}
