import assert from "node:assert/strict";
import test from "node:test";
import {
  clearComposer,
  completeSlashCommand,
  createComposerState,
  deleteComposerText,
  deleteComposerToLineStart,
  deleteComposerWord,
  insertComposerText,
  moveComposerCursor,
  nextComposerHistory,
  previousComposerHistory,
  slashSuggestions,
  submitComposer,
} from "../dist/tui/composer.js";
import { SLASH_COMMANDS } from "../dist/tui/commands.js";

test("composer inserts multiline text at its cursor and supports local editing", () => {
  let state = createComposerState();
  state = insertComposerText(state, "first\r\nthird");
  state = moveComposerCursor(state, "left");
  state = moveComposerCursor(state, "left");
  state = moveComposerCursor(state, "left");
  state = moveComposerCursor(state, "left");
  state = moveComposerCursor(state, "left");
  state = insertComposerText(state, "second\n");

  assert.equal(state.value, "first\nsecond\nthird");
  assert.equal(state.cursor, "first\nsecond\n".length);

  state = deleteComposerText(state, "backward");
  assert.equal(state.value, "first\nsecondthird");
  state = deleteComposerText(state, "forward");
  assert.equal(state.value, "first\nsecondhird");
  assert.equal(clearComposer(state).value, "");
});

test("composer erases whole words and back to the start of the current line", () => {
  let state = createComposerState();
  state = insertComposerText(state, "alpha beta  gamma");

  state = deleteComposerWord(state);
  assert.equal(state.value, "alpha beta  ");
  state = deleteComposerWord(state);
  assert.equal(state.value, "alpha ");
  assert.equal(state.cursor, "alpha ".length);
  state = deleteComposerWord(state);
  assert.equal(state.value, "");
  // Erasing an empty composer is a no-op, not an underflow.
  assert.equal(deleteComposerWord(state).value, "");

  state = insertComposerText(state, "keep\ndrop this");
  state = deleteComposerToLineStart(state);
  assert.equal(state.value, "keep\n");
  assert.equal(state.cursor, "keep\n".length);
  // The cursor already sits at the line start, so there is nothing to erase.
  assert.equal(deleteComposerToLineStart(state).value, "keep\n");
});

test("composer history preserves the current draft and deduplicates adjacent submissions", () => {
  let state = createComposerState(["one", "two"]);
  state = insertComposerText(state, "draft");
  state = previousComposerHistory(state);
  assert.equal(state.value, "two");
  state = previousComposerHistory(state);
  assert.equal(state.value, "one");
  state = nextComposerHistory(state);
  assert.equal(state.value, "two");
  state = nextComposerHistory(state);
  assert.equal(state.value, "draft");

  const first = submitComposer(insertComposerText(clearComposer(state), "  two  "));
  assert.equal(first.submitted, "two");
  assert.deepEqual(first.state.history, ["one", "two"]);
});

test("slash suggestions and Tab completion cycle through a stable prefix", () => {
  let state = insertComposerText(createComposerState(), "/skill-");
  assert.deepEqual(
    slashSuggestions(state, SLASH_COMMANDS).map((command) => command.name),
    ["skill-trust", "skill-disable", "skill-untrust"],
  );

  state = completeSlashCommand(state, SLASH_COMMANDS);
  assert.equal(state.value, "/skill-trust ");
  state = completeSlashCommand(state, SLASH_COMMANDS);
  assert.equal(state.value, "/skill-disable ");
  state = completeSlashCommand(state, SLASH_COMMANDS, true);
  assert.equal(state.value, "/skill-trust ");

  state = insertComposerText(state, "demo");
  assert.deepEqual(slashSuggestions(state, SLASH_COMMANDS), []);
});

test("empty composer submission is ignored and cleared", () => {
  const result = submitComposer(insertComposerText(createComposerState(), " \n "));
  assert.equal(result.submitted, undefined);
  assert.equal(result.state.value, "");
});

test("composer cursor and deletion preserve emoji code points", () => {
  let state = insertComposerText(createComposerState(), "a🙂b");
  state = moveComposerCursor(state, "left");
  state = moveComposerCursor(state, "left");
  assert.equal(state.cursor, 1);
  state = deleteComposerText(state, "forward");
  assert.equal(state.value, "ab");

  state = insertComposerText(clearComposer(state), "🙂");
  state = deleteComposerText(state, "backward");
  assert.equal(state.value, "");
  assert.equal(state.cursor, 0);
});
