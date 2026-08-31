import assert from "node:assert/strict";
import test from "node:test";
import {
  PROTOCOL_VERSION,
  isApprovalDescriptor,
  isConfigDescriptor,
  isEventNotification,
  isInitializeResult,
  isRunAccepted,
  isRunDescriptor,
  isRunErrorNotification,
  isSessionDescriptor,
  isSessionEventsResult,
  isTaskDescriptor,
} from "@aihi/code-protocol";

test("protocol 0.3 freezes authority-aware Worker envelopes", () => {
  assert.equal(PROTOCOL_VERSION, "0.3");
  assert.equal(isInitializeResult({
    protocol_version: "0.3",
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
    execution: { transport: "sandbox", sandbox: { name: "host" } },
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
  assert.equal(isApprovalDescriptor({
    approval_id: "approval_1",
    scope: "process.exec",
    granted_by: "policy",
    expires_at: null,
    run_id: "run_1",
    one_shot: false,
    execution: "not an object",
  }), false);

  assert.equal(isRunErrorNotification({
    jsonrpc: "2.0",
    method: "run.error",
    params: {
      protocol_version: "0.3",
      session_id: "ses_1",
      run_id: "run_1",
      message: "failed before start",
    },
  }), true);
  assert.equal(isRunErrorNotification({
    jsonrpc: "2.0",
    method: "run.error",
    params: { protocol_version: "0.3", run_id: "run_1", message: "missing session" },
  }), false);

  assert.equal(isEventNotification({
    jsonrpc: "2.0",
    method: "event",
    params: {
      protocol_version: "0.3",
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

  assert.equal(isConfigDescriptor({
    source_path: null,
    source_paths: [],
    base_dir: "/config",
    provider: { name: "fake", model: "demo" },
    providers: [{ name: "fake", model: "demo" }],
    tools: ["read_file"],
    access_mode: "workspace_write",
    run_mode: "execute",
    command_sandbox: { backend: "host", unsafe: true },
    skills: {},
    mcp_servers: [],
    audit: { enabled: false, path: null },
  }), true);
  assert.equal(isConfigDescriptor({
    provider: { name: "fake", model: "demo" },
    providers: [],
    tools: [],
    permission_mode: "default",
    sandbox: { backend: "host", root: "/workspace", unsafe: true },
  }), false);
  assert.equal(isSessionDescriptor({
    session_id: "ses_1",
    cwd: "/workspace",
    head_seq: 1,
    created_at: "2026-08-12T00:00:00Z",
    metadata: {},
    parent_session_id: null,
  }), true);
  assert.equal(isRunDescriptor({
    run_id: "run_1",
    state: "completed",
    started_at: "2026-08-12T00:00:00Z",
    updated_at: "2026-08-12T00:00:01Z",
    provider: "fake",
    model: "demo",
    access_mode: "workspace_write",
    run_mode: "execute",
    error: null,
    pending_approval_id: null,
  }), true);
  assert.equal(isTaskDescriptor({
    spec: {
      parent_run_id: "run_parent",
      objective: "inspect",
      budget: {
        max_tokens: 100,
        max_cost_usd: null,
        timeout_seconds: 10,
        max_tool_calls: 2,
      },
      task_id: "task_1",
      child_run_id: "run_child",
      parent_task_id: null,
      constraints: [],
      capabilities: ["filesystem.read"],
      depth: 0,
      max_depth: 1,
      max_children: 2,
      metadata: {},
      created_at: "2026-08-12T00:00:00Z",
    },
    state: "pending",
    child_task_ids: [],
    result: null,
    reason: null,
    updated_at: "2026-08-12T00:00:00Z",
  }), true);
  assert.equal(isTaskDescriptor({
    spec: { workspace: { root: "/workspace" } },
    state: "pending",
    child_task_ids: [],
    result: null,
    reason: null,
    updated_at: "2026-08-12T00:00:00Z",
  }), false);
});
