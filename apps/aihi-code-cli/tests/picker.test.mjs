import assert from "node:assert/strict";
import test from "node:test";
import {
  filterPickerOptions,
  modelPickerOptions,
  movePickerSelection,
  providerPickerOptions,
  sessionPickerOptions,
} from "../dist/tui/picker.js";

const providers = [
  { name: "openai", model: "gpt-4o", models: ["gpt-4o", "gpt-4.1"], base_url: "https://api.openai.com/v1" },
  { name: "deepseek", model: "deepseek-chat", models: ["deepseek-chat", "deepseek-reasoner"], base_url: "https://api.deepseek.com/v1" },
];

test("picker options are searchable across labels and details", () => {
  const options = providerPickerOptions(providers);
  assert.deepEqual(filterPickerOptions(options, "deep chat").map((item) => item.key), ["deepseek"]);
  assert.deepEqual(filterPickerOptions(modelPickerOptions(providers), "openai gpt").map((item) => item.key), ["openai/gpt-4o", "openai/gpt-4.1"]);
  assert.deepEqual(modelPickerOptions(providers).map((item) => item.key), [
    "openai/gpt-4o",
    "openai/gpt-4.1",
    "deepseek/deepseek-chat",
    "deepseek/deepseek-reasoner",
  ]);
  assert.deepEqual(filterPickerOptions(options, "").map((item) => item.key), ["openai", "deepseek"]);
});

test("session picker includes stable id, workspace, provider, model, and sequence", () => {
  const options = sessionPickerOptions([{
    session_id: "session_1234567890",
    head_seq: 7,
    created_at: "2026-08-12T00:00:00Z",
    metadata: { title: "Fix auth", cwd: "/tmp/project", provider: "deepseek", model: "deepseek-chat" },
    parent_session_id: null,
  }]);
  assert.equal(options[0].value, "session_1234567890");
  assert.match(options[0].detail, /deepseek\/deepseek-chat/);
  assert.match(options[0].detail, /seq 7/);
  assert.match(options[0].searchText, /Fix auth/);
});

test("picker selection wraps and stays empty-safe", () => {
  assert.equal(movePickerSelection(0, 3, "up"), 2);
  assert.equal(movePickerSelection(2, 3, "down"), 0);
  assert.equal(movePickerSelection(4, 0, "down"), 0);
});
