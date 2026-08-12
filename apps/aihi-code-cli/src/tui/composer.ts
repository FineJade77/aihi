export const COMPOSER_MAX_LENGTH = 32_000;
export const COMPOSER_HISTORY_LIMIT = 100;

export interface SlashCommandDescriptor {
  name: string;
  usage: string;
  description: string;
}

interface CompletionCycle {
  query: string;
  index: number;
  suffix: string;
}

export interface ComposerState {
  value: string;
  cursor: number;
  history: string[];
  historyIndex?: number;
  draft: string;
  completion?: CompletionCycle;
}

export interface ComposerSubmission {
  state: ComposerState;
  submitted?: string;
}

export function createComposerState(history: readonly string[] = []): ComposerState {
  return {
    value: "",
    cursor: 0,
    history: history.slice(-COMPOSER_HISTORY_LIMIT),
    draft: "",
  };
}

export function insertComposerText(state: ComposerState, rawText: string): ComposerState {
  const text = rawText.replace(/\r\n?/g, "\n");
  const room = COMPOSER_MAX_LENGTH - state.value.length;
  if (room <= 0 || text.length === 0) return resetCompletion(state);
  const inserted = fittedText(text, room);
  if (inserted.length === 0) return resetCompletion(state);
  return edited(state, {
    value: `${state.value.slice(0, state.cursor)}${inserted}${state.value.slice(state.cursor)}`,
    cursor: state.cursor + inserted.length,
  });
}

export function moveComposerCursor(
  state: ComposerState,
  direction: "left" | "right",
): ComposerState {
  return resetCompletion({
    ...state,
    cursor: direction === "left"
      ? previousCodePointIndex(state.value, state.cursor)
      : nextCodePointIndex(state.value, state.cursor),
  });
}

export function deleteComposerText(
  state: ComposerState,
  direction: "backward" | "forward",
): ComposerState {
  if (direction === "backward") {
    if (state.cursor === 0) return resetCompletion(state);
    const previous = previousCodePointIndex(state.value, state.cursor);
    return edited(state, {
      value: `${state.value.slice(0, previous)}${state.value.slice(state.cursor)}`,
      cursor: previous,
    });
  }
  if (state.cursor >= state.value.length) return resetCompletion(state);
  const next = nextCodePointIndex(state.value, state.cursor);
  return edited(state, {
    value: `${state.value.slice(0, state.cursor)}${state.value.slice(next)}`,
    cursor: state.cursor,
  });
}

/** Ctrl-W / Option-Backspace: drop the whitespace run, then the word before it. */
export function deleteComposerWord(state: ComposerState): ComposerState {
  if (state.cursor === 0) return resetCompletion(state);
  const before = state.value.slice(0, state.cursor);
  const trimmed = before.replace(/[^\s]*\s*$/, "");
  return edited(state, {
    value: `${trimmed}${state.value.slice(state.cursor)}`,
    cursor: trimmed.length,
  });
}

/** Ctrl-U: drop the current line up to the cursor, keeping earlier lines. */
export function deleteComposerToLineStart(state: ComposerState): ComposerState {
  const lineStart = state.value.lastIndexOf("\n", state.cursor - 1) + 1;
  if (lineStart === state.cursor) return resetCompletion(state);
  return edited(state, {
    value: `${state.value.slice(0, lineStart)}${state.value.slice(state.cursor)}`,
    cursor: lineStart,
  });
}

export function clearComposer(state: ComposerState): ComposerState {
  return {
    ...state,
    value: "",
    cursor: 0,
    historyIndex: undefined,
    draft: "",
    completion: undefined,
  };
}

export function previousComposerHistory(state: ComposerState): ComposerState {
  if (state.history.length === 0) return state;
  const index = state.historyIndex === undefined
    ? state.history.length - 1
    : Math.max(0, state.historyIndex - 1);
  const value = state.history[index] ?? "";
  return {
    ...state,
    value,
    cursor: value.length,
    historyIndex: index,
    draft: state.historyIndex === undefined ? state.value : state.draft,
    completion: undefined,
  };
}

export function nextComposerHistory(state: ComposerState): ComposerState {
  if (state.historyIndex === undefined) return state;
  const index = state.historyIndex + 1;
  if (index >= state.history.length) {
    return {
      ...state,
      value: state.draft,
      cursor: state.draft.length,
      historyIndex: undefined,
      completion: undefined,
    };
  }
  const value = state.history[index] ?? "";
  return {
    ...state,
    value,
    cursor: value.length,
    historyIndex: index,
    completion: undefined,
  };
}

export function slashSuggestions(
  state: ComposerState,
  commands: readonly SlashCommandDescriptor[],
  limit = 5,
): SlashCommandDescriptor[] {
  const query = state.completion?.query ?? slashQuery(state.value, state.cursor);
  if (query === undefined) return [];
  return matchingCommands(query, commands).slice(0, Math.max(0, limit));
}

export function completeSlashCommand(
  state: ComposerState,
  commands: readonly SlashCommandDescriptor[],
  backwards = false,
): ComposerState {
  const existing = state.completion;
  const query = existing?.query ?? slashQuery(state.value, state.cursor);
  if (query === undefined) return state;
  const candidates = matchingCommands(query, commands).slice(0, 5);
  if (candidates.length === 0) return state;
  const suffix = existing?.suffix ?? state.value.slice(state.cursor);
  const previous = existing?.index ?? (backwards ? 0 : -1);
  const index = (previous + (backwards ? -1 : 1) + candidates.length) % candidates.length;
  const insertion = `/${candidates[index].name} `;
  return {
    ...state,
    value: `${insertion}${suffix}`.slice(0, COMPOSER_MAX_LENGTH),
    cursor: insertion.length,
    historyIndex: undefined,
    completion: { query, index, suffix },
  };
}

export function submitComposer(state: ComposerState): ComposerSubmission {
  const submitted = state.value.trim();
  if (!submitted) return { state: clearComposer(state) };
  const history = state.history[state.history.length - 1] === submitted
    ? state.history
    : [...state.history, submitted].slice(-COMPOSER_HISTORY_LIMIT);
  return {
    submitted,
    state: {
      value: "",
      cursor: 0,
      history,
      draft: "",
    },
  };
}

function edited(
  state: ComposerState,
  patch: Pick<ComposerState, "value" | "cursor">,
): ComposerState {
  return {
    ...state,
    ...patch,
    historyIndex: undefined,
    draft: "",
    completion: undefined,
  };
}

function resetCompletion(state: ComposerState): ComposerState {
  return state.completion === undefined ? state : { ...state, completion: undefined };
}

function slashQuery(value: string, cursor: number): string | undefined {
  if (!value.startsWith("/") || value.includes("\n")) return undefined;
  const beforeCursor = value.slice(0, cursor);
  if (/\s/.test(beforeCursor) || !beforeCursor.startsWith("/")) return undefined;
  return beforeCursor.slice(1).toLowerCase();
}

function matchingCommands(
  query: string,
  commands: readonly SlashCommandDescriptor[],
): SlashCommandDescriptor[] {
  return commands.filter((command) => command.name.toLowerCase().startsWith(query));
}

function previousCodePointIndex(value: string, cursor: number): number {
  const previous = Math.max(0, cursor - 1);
  const unit = value.charCodeAt(previous);
  if (unit >= 0xdc00 && unit <= 0xdfff && previous > 0) {
    const leading = value.charCodeAt(previous - 1);
    if (leading >= 0xd800 && leading <= 0xdbff) return previous - 1;
  }
  return previous;
}

function nextCodePointIndex(value: string, cursor: number): number {
  if (cursor >= value.length) return value.length;
  const unit = value.charCodeAt(cursor);
  if (unit >= 0xd800 && unit <= 0xdbff && cursor + 1 < value.length) {
    const trailing = value.charCodeAt(cursor + 1);
    if (trailing >= 0xdc00 && trailing <= 0xdfff) return cursor + 2;
  }
  return cursor + 1;
}

function fittedText(value: string, room: number): string {
  let result = "";
  for (const codePoint of value) {
    if (result.length + codePoint.length > room) break;
    result += codePoint;
  }
  return result;
}
