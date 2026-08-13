import assert from "node:assert/strict";
import { resolve } from "node:path";
import test from "node:test";
import { bootstrapSession } from "../dist/bootstrap.js";
import { defaultStorePath, parseArgs } from "../dist/main.js";

const config = {
  source_path: "/home/test/.aihi/aihi-code.toml",
  source_paths: ["/home/test/.aihi/aihi-code.toml"],
  base_dir: "/workspace",
  provider: { name: "deepseek", model: "deepseek-chat", models: ["deepseek-chat", "deepseek-reasoner"] },
  providers: [
    { name: "deepseek", model: "deepseek-chat", models: ["deepseek-chat", "deepseek-reasoner"] },
    { name: "openai", model: "gpt-5", models: ["gpt-5", "gpt-5-mini"] },
  ],
  tools: [],
  sandbox: { backend: "host", root: "/workspace", unsafe: true },
  skills: {},
  mcp_servers: [],
};

function session(id, cwd, provider = "deepseek", model = "deepseek-chat") {
  return {
    session_id: id,
    head_seq: 1,
    created_at: "2026-08-12T00:00:00Z",
    metadata: { cwd, provider, model },
    parent_session_id: null,
  };
}

test("bootstrap loads config before creating a session and uses Worker defaults", async () => {
  const cwd = resolve("project");
  const calls = [];
  const client = {
    async getConfig(receivedCwd) {
      calls.push(["config", receivedCwd]);
      return config;
    },
    async createSession(params) {
      calls.push(["create", params]);
      return session("ses_new", cwd);
    },
    async getSession() {
      throw new Error("unexpected getSession");
    },
    async listSessions() {
      throw new Error("unexpected listSessions");
    },
  };

  const result = await bootstrapSession(client, { cwd });

  assert.deepEqual(calls, [
    ["config", cwd],
    ["create", { cwd }],
  ]);
  assert.equal(result.session.session_id, "ses_new");
  assert.equal(result.provider, "deepseek");
  assert.equal(result.model, "deepseek-chat");
  assert.equal(result.resumed, false);
});

test("--continue selects the newest session belonging to the workspace", async () => {
  const cwd = resolve("project");
  const client = {
    async getConfig() {
      return config;
    },
    async createSession() {
      throw new Error("must not create a session");
    },
    async getSession() {
      throw new Error("unexpected getSession");
    },
    async listSessions(limit) {
      assert.equal(limit, 100);
      return [session("ses_other", resolve("other")), session("ses_latest", cwd)];
    },
  };

  const result = await bootstrapSession(client, { cwd, continueSession: true });

  assert.equal(result.session.session_id, "ses_latest");
  assert.equal(result.resumed, true);
});

test("provider override is resolved before the TUI mounts", async () => {
  const cwd = resolve("project");
  let createParams;
  const client = {
    async getConfig() {
      return config;
    },
    async createSession(params) {
      createParams = params;
      return session("ses_openai", cwd, "openai", "gpt-5");
    },
    async getSession() {
      throw new Error("unexpected getSession");
    },
    async listSessions() {
      throw new Error("unexpected listSessions");
    },
  };

  const result = await bootstrapSession(client, { cwd, provider: "openai" });

  assert.deepEqual(createParams, { cwd, provider: "openai" });
  assert.equal(result.provider, "openai");
  assert.equal(result.model, "gpt-5");
});

test("bootstrap rejects a model that is not configured for the selected provider", async () => {
  const cwd = resolve("project");
  const client = {
    async getConfig() { return config; },
    async createSession() { throw new Error("unexpected createSession"); },
    async getSession() { throw new Error("unexpected getSession"); },
    async listSessions() { throw new Error("unexpected listSessions"); },
  };

  await assert.rejects(
    bootstrapSession(client, { cwd, provider: "openai", model: "not-configured" }),
    /Model is not configured for provider openai/,
  );
});

test("an explicit session cannot silently switch workspaces", async () => {
  const cwd = resolve("project");
  const client = {
    async getConfig() {
      return config;
    },
    async createSession() {
      throw new Error("unexpected createSession");
    },
    async getSession() {
      return session("ses_other", resolve("other"));
    },
    async listSessions() {
      throw new Error("unexpected listSessions");
    },
  };

  await assert.rejects(
    bootstrapSession(client, { cwd, sessionId: "ses_other" }),
    /belongs to another workspace/,
  );
});

test("CLI defaults to the user session database and rejects ambiguous resume flags", () => {
  assert.equal(defaultStorePath("/home/test"), "/home/test/.aihi/sessions.sqlite3");
  assert.equal(parseArgs(["--continue"]).continueSession, true);
  assert.throws(
    () => parseArgs(["--continue", "--session", "ses_1"]),
    /cannot be used together/,
  );
});
