import { Box, Text, useInput } from "ink";
import {
  clearComposer,
  completeSlashCommand,
  deleteComposerText,
  insertComposerText,
  moveComposerCursor,
  nextComposerHistory,
  previousComposerHistory,
  slashSuggestions,
  submitComposer,
  type ComposerState,
  type SlashCommandDescriptor,
} from "./composer.js";

export interface ComposerInputProps {
  state: ComposerState;
  commands: readonly SlashCommandDescriptor[];
  active?: boolean;
  onChange: (state: ComposerState) => void;
  onSubmit: (value: string) => void;
}

/** Ink keyboard adapter around the pure Composer state transitions. */
export function ComposerInput({
  state,
  commands,
  active = true,
  onChange,
  onSubmit,
}: ComposerInputProps) {
  useInput((input, key) => {
    const multilineShortcut = key.ctrl && input === "j";
    if (key.escape) {
      onChange(clearComposer(state));
      return;
    }
    const tabShortcut = key.tab || input === "\t" || input === "tab";
    if ((key.ctrl || key.meta) && !multilineShortcut) return;
    if (tabShortcut) {
      onChange(completeSlashCommand(state, commands, key.shift));
      return;
    }
    if (key.upArrow) {
      onChange(previousComposerHistory(state));
      return;
    }
    if (key.downArrow) {
      onChange(nextComposerHistory(state));
      return;
    }
    if (key.leftArrow) {
      onChange(moveComposerCursor(state, "left"));
      return;
    }
    if (key.rightArrow) {
      onChange(moveComposerCursor(state, "right"));
      return;
    }
    if (key.backspace) {
      onChange(deleteComposerText(state, "backward"));
      return;
    }
    if (key.delete) {
      onChange(deleteComposerText(state, "forward"));
      return;
    }
    if (key.return) {
      const submission = submitComposer(state);
      onChange(submission.state);
      if (submission.submitted !== undefined) onSubmit(submission.submitted);
      return;
    }
    // Depending on the terminal, Ctrl-J arrives as either ctrl+j or a bare LF.
    if (multilineShortcut || input === "\n") {
      onChange(insertComposerText(state, "\n"));
      return;
    }
    if (!key.ctrl && !key.meta && input) {
      onChange(insertComposerText(state, input));
    }
  }, { isActive: active });

  const window = composerWindow(state);
  const before = window.value.slice(0, window.cursor);
  const cursor = Array.from(window.value.slice(window.cursor))[0] ?? "";
  const after = window.value.slice(window.cursor + cursor.length);
  const renderSegment = (value: string): string => value.replace(/\n/g, "\n  ");
  const suggestions = slashSuggestions(state, commands);
  const selectedSuggestion = state.completion?.index ?? 0;
  return (
    <Box flexDirection="column">
      <Box>
        <Text color="magenta">› </Text>
        <Text>
          {renderSegment(before)}
          <Text inverse>{cursor || " "}</Text>
          {renderSegment(after)}
        </Text>
      </Box>
      {state.value.includes("\n") && (
        <Text color="gray">Ctrl-J newline · Enter send</Text>
      )}
      {suggestions.length > 0 && (
        <Box flexDirection="column" paddingLeft={2}>
          {suggestions.map((command, index) => (
            <Text
              key={command.name}
              color={index === selectedSuggestion ? "magenta" : "gray"}
            >
              {command.usage} · {command.description}
            </Text>
          ))}
          <Text color="gray">Tab / Shift-Tab complete</Text>
        </Box>
      )}
    </Box>
  );
}

function composerWindow(state: ComposerState): { value: string; cursor: number } {
  const lines = state.value.split("\n");
  if (lines.length <= 3) return { value: state.value, cursor: state.cursor };
  let consumed = 0;
  let cursorLine = 0;
  for (let index = 0; index < lines.length; index += 1) {
    const end = consumed + (lines[index]?.length ?? 0);
    if (state.cursor <= end || index === lines.length - 1) {
      cursorLine = index;
      break;
    }
    consumed = end + 1;
  }
  const startLine = Math.min(
    Math.max(0, cursorLine - 1),
    Math.max(0, lines.length - 3),
  );
  const endLine = Math.min(lines.length, startLine + 3);
  const startOffset = lines.slice(0, startLine).reduce(
    (total, line) => total + line.length + 1,
    0,
  );
  const prefix = startLine > 0 ? "… " : "";
  const suffix = endLine < lines.length ? " …" : "";
  return {
    value: `${prefix}${lines.slice(startLine, endLine).join("\n")}${suffix}`,
    cursor: prefix.length + Math.max(0, state.cursor - startOffset),
  };
}
