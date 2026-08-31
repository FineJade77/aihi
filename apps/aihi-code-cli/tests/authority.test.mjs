import assert from "node:assert/strict";
import test from "node:test";
import {
  authorityFromEventData,
  configuredAuthority,
  effectiveAuthority,
  formatAuthority,
} from "../dist/tui/authority.js";

const config = {
  access_mode: "workspace_write",
  run_mode: "execute",
};

test("CLI authority display prefers persisted Run modes", () => {
  const configured = configuredAuthority(config);
  assert.deepEqual(configured, {
    accessMode: "workspace_write",
    runMode: "execute",
  });
  assert.equal(formatAuthority(configured), "workspace_write · execute");

  const runs = [
    { run_id: "run_latest", access_mode: "read_only", run_mode: "plan" },
    { run_id: "run_active", access_mode: "full_access", run_mode: "execute" },
  ];
  assert.deepEqual(effectiveAuthority(config, runs, "run_active"), {
    accessMode: "full_access",
    runMode: "execute",
  });
  assert.deepEqual(effectiveAuthority(config, runs), {
    accessMode: "read_only",
    runMode: "plan",
  });
});

test("live Run profile updates authority without trusting malformed values", () => {
  const current = configuredAuthority(config);
  assert.deepEqual(authorityFromEventData({
    application_profile: {
      access_mode: "read_only",
      run_mode: "plan",
    },
  }, current), {
    accessMode: "read_only",
    runMode: "plan",
  });
  assert.deepEqual(authorityFromEventData({
    application_profile: {
      access_mode: "anything",
      run_mode: "execute",
    },
  }, current), current);
});
