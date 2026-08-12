import assert from "node:assert/strict";
import test from "node:test";
import {
  TranscriptGapError,
  appendTranscriptEvent,
  mergeTranscriptEvents,
  projectTranscript,
  transcriptEventFromNotification,
} from "../dist/transcript.js";

const SESSION_ID = "ses_transcript";
const RUN_ID = "run_1";

function record(seq, type, data = {}) {
  return {
    id: `evt_${seq}`,
    type,
    session_id: SESSION_ID,
    run_id: RUN_ID,
    seq,
    created_at: "2026-08-12T00:00:00Z",
    ephemeral: false,
    schema_version: 1,
    data,
  };
}

function message(role, id, content) {
  return { message: { id, role, content, metadata: {} } };
}

test("replay projects user, assistant, and one evolving tool entry", () => {
  const projection = projectTranscript([
    record(1, "session.created"),
    record(2, "user.message", message("user", "msg_user", [
      { kind: "text", text: "inspect the repository" },
    ])),
    record(3, "run.started", { provider: "deepseek", model: "deepseek-chat" }),
    record(4, "assistant.message", message("assistant", "msg_call", [
      { kind: "text", text: "I will inspect it." },
      {
        kind: "tool_call",
        id: "call_1",
        name: "bash",
        input: {
          command: "git status -- OPENAI_API_KEY=must-not-render Authorization: Bearer second-secret --token third-secret",
          api_key: "must-not-render",
        },
      },
    ])),
    record(5, "tool.requested", {
      tool_call_id: "call_1",
      tool_name: "bash",
      input: {
        command: "git status -- OPENAI_API_KEY=must-not-render Authorization: Bearer second-secret --token third-secret",
        api_key: "must-not-render",
      },
    }),
    record(6, "tool.started", { tool_call_id: "call_1", tool_name: "bash" }),
    record(7, "tool.completed", {
      tool_call_id: "call_1",
      tool_name: "bash",
      is_error: false,
    }),
    record(8, "tool.result", message("user", "msg_result", [
      {
        kind: "tool_result",
        tool_call_id: "call_1",
        content: "working tree clean",
        is_error: false,
        metadata: {},
      },
    ])),
    record(9, "assistant.message", message("assistant", "msg_answer", [
      { kind: "text", text: "The repository is clean." },
    ])),
  ]);

  assert.equal(projection.headSeq, 9);
  assert.deepEqual(projection.entries.map((entry) => entry.kind), [
    "user",
    "assistant",
    "tool",
    "assistant",
  ]);
  const tool = projection.entries[2];
  assert.equal(tool.toolCallId, "call_1");
  assert.equal(tool.toolName, "bash");
  assert.equal(tool.status, "succeeded");
  assert.equal(
    tool.text,
    "$ git status -- OPENAI_API_KEY=[REDACTED] Authorization=[REDACTED] --token=[REDACTED]",
  );
  assert.equal(tool.detail, "working tree clean");
  assert.equal(JSON.stringify(tool).includes("must-not-render"), false);
  assert.equal(JSON.stringify(tool).includes("second-secret"), false);
  assert.equal(JSON.stringify(tool).includes("third-secret"), false);
  assert.equal(tool.seq, 4);
  assert.equal(tool.updatedSeq, 8);
});

test("approval resolution updates its existing tool call without a duplicate row", () => {
  const projection = projectTranscript([
    record(1, "session.created"),
    record(2, "assistant.message", message("assistant", "msg_call", [
      { kind: "tool_call", id: "call_1", name: "write_file", input: { path: "a.txt" } },
    ])),
    record(3, "approval.requested", {
      approval: { approval_id: "approval_1" },
      tool_call_id: "call_1",
      tool_name: "write_file",
      reason: "filesystem write requires approval",
    }),
    record(4, "approval.resolved", {
      approval_id: "approval_1",
      status: "denied",
    }),
  ]);

  assert.equal(projection.entries.length, 1);
  assert.equal(projection.entries[0].status, "denied");
  assert.equal(projection.entries[0].isError, true);
  assert.equal(projection.entries[0].detail, "filesystem write requires approval");
  assert.equal(projection.entries[0].updatedSeq, 4);
});

test("live durable notifications share replay ordering and ignore duplicates", () => {
  const initial = projectTranscript([
    record(1, "session.created"),
    record(2, "user.message", message("user", "msg_user", [
      { kind: "text", text: "hello" },
    ])),
  ]);
  const duplicate = transcriptEventFromNotification({
    session_id: SESSION_ID,
    run_id: RUN_ID,
    event_type: "user.message",
    seq: 2,
    ephemeral: false,
    data: message("user", "msg_user", [{ kind: "text", text: "hello" }]),
  });
  const next = transcriptEventFromNotification({
    session_id: SESSION_ID,
    run_id: RUN_ID,
    event_type: "assistant.message",
    seq: 3,
    ephemeral: false,
    data: message("assistant", "msg_answer", [{ kind: "text", text: "hi" }]),
  });

  const unchanged = appendTranscriptEvent(initial, duplicate);
  const updated = appendTranscriptEvent(unchanged, next);

  assert.strictEqual(unchanged, initial);
  assert.equal(updated.headSeq, 3);
  assert.deepEqual(updated.entries.map((entry) => entry.text), ["hello", "hi"]);
});

test("live projection fails closed on a durable sequence gap", () => {
  const initial = projectTranscript([record(1, "session.created")]);
  const gap = transcriptEventFromNotification({
    session_id: SESSION_ID,
    run_id: RUN_ID,
    event_type: "run.completed",
    seq: 3,
    ephemeral: false,
    data: { state: "completed" },
  });

  assert.throws(() => appendTranscriptEvent(initial, gap), TranscriptGapError);
});

test("notifications buffered during replay are sorted and deduplicated", () => {
  const initial = projectTranscript([
    record(1, "session.created"),
    record(2, "user.message", message("user", "msg_user", [
      { kind: "text", text: "question" },
    ])),
  ]);
  const third = transcriptEventFromNotification({
    session_id: SESSION_ID,
    run_id: RUN_ID,
    event_type: "run.started",
    seq: 3,
    ephemeral: false,
    data: {},
  });
  const fourth = transcriptEventFromNotification({
    session_id: SESSION_ID,
    run_id: RUN_ID,
    event_type: "assistant.message",
    seq: 4,
    ephemeral: false,
    data: message("assistant", "msg_answer", [{ kind: "text", text: "answer" }]),
  });

  const merged = mergeTranscriptEvents(initial, [fourth, third, third]);

  assert.equal(merged.headSeq, 4);
  assert.equal(merged.activeRunId, RUN_ID);
  assert.deepEqual(merged.entries.map((entry) => entry.text), ["question", "answer"]);
});

test("ephemeral chunks never become durable transcript entries", () => {
  const initial = projectTranscript([record(1, "session.created")]);
  const chunk = transcriptEventFromNotification({
    session_id: SESSION_ID,
    run_id: RUN_ID,
    event_type: "model.chunk",
    ephemeral: true,
    data: { kind: "text_delta", text: "partial" },
  });

  assert.strictEqual(appendTranscriptEvent(initial, chunk), initial);
});

test("replay restores the active run and clears it at the terminal event", () => {
  const running = projectTranscript([
    record(1, "session.created"),
    record(2, "run.started", { provider: "deepseek", model: "deepseek-chat" }),
  ]);
  assert.equal(running.activeRunId, RUN_ID);

  const completed = appendTranscriptEvent(running, transcriptEventFromNotification({
    session_id: SESSION_ID,
    run_id: RUN_ID,
    event_type: "run.completed",
    seq: 3,
    ephemeral: false,
    data: { state: "completed" },
  }));

  assert.equal(completed.activeRunId, undefined);
});

test("legacy message.added events use the canonical message role", () => {
  const projection = projectTranscript([
    record(1, "session.created"),
    record(2, "message.added", message("user", "msg_legacy", [
      { kind: "text", text: "legacy question" },
    ])),
  ]);

  assert.equal(projection.entries[0].kind, "user");
  assert.equal(projection.entries[0].text, "legacy question");
});
