import assert from "node:assert/strict";
import test from "node:test";
import {
  PROTOCOL_VERSION,
  isApprovalDescriptor,
  isEventNotification,
  isInitializeResult,
  isRunAccepted,
  isRunErrorNotification,
  isSessionEventsResult,
} from "@aihi/code-protocol";

test("protocol 0.2 freezes accepted Run and notification envelopes", () => {
  assert.equal(PROTOCOL_VERSION, "0.2");
  assert.equal(isInitializeResult({
    protocol_version: "0.2",
    server_name: "aihi-code-agent",
    capabilities: { events: true, commands: [] },
  }), true);
  assert.equal(isInitializeResult({
    protocol_version: "0.1",
    server_name: "aihi-code-agent",
    capabilities: { events: true, commands: [] },
  }), false);
  assert.equal(isRunAccepted({ run_id: "run_1", accepted: true }), true);
  assert.equal(isRunAccepted({ run_id: null, accepted: true }), false);
  assert.equal(isRunAccepted({ run_id: "run_1", accepted: false }), false);
  assert.equal(isApprovalDescriptor({
    approval_id: "approval_1",
    scope: "process.exec",
    granted_by: "policy",
    requested_by: "tool-policy",
    expires_at: null,
    run_id: "run_1",
    one_shot: false,
    tool_input: { command: "git status" },
    required_capabilities: ["process.exec"],
    sandbox: { name: "host" },
  }), true);
  assert.equal(isApprovalDescriptor({
    approval_id: "approval_1",
    scope: "process.exec",
    granted_by: "policy",
    expires_at: null,
    run_id: "run_1",
    one_shot: false,
    tool_input: "not an object",
  }), false);

  assert.equal(isRunErrorNotification({
    jsonrpc: "2.0",
    method: "run.error",
    params: {
      protocol_version: "0.2",
      session_id: "ses_1",
      run_id: "run_1",
      message: "failed before start",
    },
  }), true);
  assert.equal(isRunErrorNotification({
    jsonrpc: "2.0",
    method: "run.error",
    params: { protocol_version: "0.2", run_id: "run_1", message: "missing session" },
  }), false);

  assert.equal(isEventNotification({
    jsonrpc: "2.0",
    method: "event",
    params: {
      protocol_version: "0.2",
      event: {
        session_id: "ses_1",
        run_id: "run_1",
        event_type: "model.chunk",
        ephemeral: true,
        data: { kind: "text_delta", text: "hi" },
      },
    },
  }), true);
  assert.equal(isEventNotification({
    jsonrpc: "2.0",
    method: "event",
    params: {
      protocol_version: "0.2",
      event: {
        session_id: "ses_1",
        event_type: "run.completed",
        ephemeral: false,
        data: {},
      },
    },
  }), false);
  assert.equal(isEventNotification({
    jsonrpc: "2.0",
    method: "event",
    params: {
      protocol_version: "0.1",
      event: {
        session_id: "ses_1",
        event_type: "model.chunk",
        ephemeral: true,
        data: {},
      },
    },
  }), false);

  assert.equal(isSessionEventsResult({
    session_id: "ses_1",
    events: [],
    head_seq: 0,
    next_after_seq: 0,
    has_more: false,
  }), true);
  assert.equal(isSessionEventsResult({
    session_id: "ses_1",
    events: [],
    head_seq: 0,
    next_after_seq: 0,
    has_more: "no",
  }), false);
});
