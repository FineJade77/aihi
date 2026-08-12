import assert from "node:assert/strict";
import test from "node:test";
import {
  buildTranscriptLines,
  createViewportState,
  followTranscriptTail,
  scrollTranscriptViewport,
  selectTranscriptViewport,
  toggleToolDetails,
  wrapTerminalText,
} from "../dist/tui/viewport.js";

function entry(id, kind, text, extra = {}) {
  return { id, kind, text, seq: 1, updatedSeq: 1, ...extra };
}

test("terminal wrapping accounts for newlines and wide CJK cells", () => {
  assert.deepEqual(wrapTerminalText("abcd\n你好ab", 4), ["abcd", "你好", "ab"]);
});

test("tail viewport honors its row budget and reports hidden lines", () => {
  const lines = Array.from({ length: 12 }, (_, index) => ({
    id: String(index),
    text: `line ${index}`,
    tone: "normal",
  }));
  const viewport = selectTranscriptViewport(lines, createViewportState(), 5);

  assert.equal(viewport.followingTail, true);
  assert.equal(viewport.lines.length + (viewport.hiddenAbove > 0 ? 1 : 0), 5);
  assert.equal(viewport.lines.at(-1)?.text, "line 11");
  assert.ok(viewport.hiddenAbove > 0);
  assert.equal(viewport.hiddenBelow, 0);
});

test("page scrolling pauses tail following and can resume it", () => {
  const lines = Array.from({ length: 20 }, (_, index) => ({
    id: String(index),
    text: `line ${index}`,
    tone: "normal",
  }));
  const initial = createViewportState();
  const up = scrollTranscriptViewport(initial, lines, 6, "up");
  const paused = selectTranscriptViewport(lines, up, 6);
  assert.equal(paused.followingTail, false);
  assert.ok(paused.hiddenBelow > 0);

  const down = scrollTranscriptViewport(up, lines, 6, "down");
  assert.ok(selectTranscriptViewport(lines, down, 6).endLine > paused.endLine);
  assert.equal(followTranscriptTail(down).endLine, undefined);
});

test("top viewport reserves rows for both newer and earlier indicators", () => {
  const lines = Array.from({ length: 20 }, (_, index) => ({
    id: String(index),
    text: `line ${index}`,
    tone: "normal",
  }));
  const state = { expandedToolDetails: false, endLine: 10 };
  const viewport = selectTranscriptViewport(lines, state, 5);

  assert.ok(viewport.hiddenAbove > 0);
  assert.ok(viewport.hiddenBelow > 0);
  assert.equal(viewport.lines.length + 2, 5);
});

test("tool results are collapsed by default and expand without changing entries", () => {
  const entries = [entry("tool:1", "tool", "$ git status", {
    toolName: "bash",
    status: "succeeded",
    detail: "working tree clean",
  })];
  const collapsedState = createViewportState();
  const collapsed = buildTranscriptLines(entries, "", 80, collapsedState.expandedToolDetails);
  assert.ok(collapsed.some((line) => line.text.includes("result hidden")));
  assert.equal(collapsed.some((line) => line.text.includes("working tree clean")), false);

  const expandedState = toggleToolDetails(collapsedState);
  const expanded = buildTranscriptLines(entries, "", 80, expandedState.expandedToolDetails);
  assert.ok(expanded.some((line) => line.text.includes("working tree clean")));
  assert.deepEqual(entries[0].detail, "working tree clean");
});

test("streaming text is represented after canonical transcript lines", () => {
  const lines = buildTranscriptLines(
    [entry("message:1", "user", "hello")],
    "partial answer",
    80,
    false,
  );
  assert.equal(lines[0].text, "YOU");
  assert.equal(lines.at(-2)?.text, "AIHI · streaming");
  assert.equal(lines.at(-1)?.text, "partial answer");
});
