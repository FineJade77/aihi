import assert from "node:assert/strict";
import test from "node:test";
import { resolveApprovalAndResume } from "../dist/approval.js";

for (const approved of [true, false]) {
  test(`${approved ? "approval" : "denial"} resumes the suspended run`, async () => {
    const calls = [];
    const client = {
      async resolveApproval(params) {
        calls.push(["resolve", params]);
        return {
          approval_id: params.approval_id,
          run_id: "run_1",
          approved: params.approved,
          one_shot: false,
        };
      },
      async resumeRun(params) {
        calls.push(["resume", params]);
        return { run_id: params.run_id, accepted: true };
      },
    };

    const result = await resolveApprovalAndResume(client, {
      session_id: "ses_1",
      approval_id: "approval_1",
      approved,
      resolved_by: "test",
    });

    assert.equal(result.resolution.approved, approved);
    assert.deepEqual(calls[1], ["resume", { session_id: "ses_1", run_id: "run_1" }]);
  });
}
