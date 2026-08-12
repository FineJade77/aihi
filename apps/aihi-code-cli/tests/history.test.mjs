import assert from "node:assert/strict";
import test from "node:test";
import { readSessionHistory } from "../dist/history.js";

function event(seq, type = "run.state_changed") {
  return {
    id: `evt_${seq}`,
    type,
    session_id: "ses_long",
    run_id: "run_1",
    seq,
    created_at: "2026-08-12T00:00:00Z",
    ephemeral: false,
    schema_version: 1,
    data: {},
  };
}

test("session history follows every page instead of truncating at the first page", async () => {
  const all = Array.from({ length: 1_201 }, (_, index) => event(index + 1));
  const cursors = [];
  const client = {
    async getSessionEvents(sessionId, afterSeq, limit) {
      assert.equal(sessionId, "ses_long");
      assert.equal(limit, 500);
      cursors.push(afterSeq);
      const events = all.slice(afterSeq, afterSeq + limit);
      const next = events.at(-1)?.seq ?? afterSeq;
      return {
        session_id: sessionId,
        events,
        head_seq: all.length,
        next_after_seq: next,
        has_more: next < all.length,
      };
    },
  };

  const history = await readSessionHistory(client, "ses_long");

  assert.deepEqual(cursors, [0, 500, 1_000]);
  assert.equal(history.events.length, 1_201);
  assert.equal(history.headSeq, 1_201);
});

test("session history fails closed when a page claims more data without advancing", async () => {
  const client = {
    async getSessionEvents(sessionId, afterSeq) {
      return {
        session_id: sessionId,
        events: [],
        head_seq: 10,
        next_after_seq: afterSeq,
        has_more: true,
      };
    },
  };

  await assert.rejects(readSessionHistory(client, "ses_stuck"), /did not advance/);
});

test("session history catches an event committed while the final page was read", async () => {
  let calls = 0;
  const client = {
    async getSessionEvents(sessionId, afterSeq) {
      calls += 1;
      if (calls === 1) {
        return {
          session_id: sessionId,
          events: [],
          head_seq: 1,
          next_after_seq: afterSeq,
          has_more: false,
        };
      }
      return {
        session_id: sessionId,
        events: [event(1)],
        head_seq: 1,
        next_after_seq: 1,
        has_more: false,
      };
    },
  };

  const history = await readSessionHistory(client, "ses_long");

  assert.equal(calls, 2);
  assert.deepEqual(history.events.map((item) => item.seq), [1]);
  assert.equal(history.headSeq, 1);
});

test("session history rejects a peer whose reported tail never becomes readable", async () => {
  const client = {
    async getSessionEvents(sessionId, afterSeq) {
      return {
        session_id: sessionId,
        events: [],
        head_seq: 1,
        next_after_seq: afterSeq,
        has_more: false,
      };
    },
  };

  await assert.rejects(readSessionHistory(client, "ses_stuck"), /did not advance/);
});

test("session history rejects an event from another session", async () => {
  const client = {
    async getSessionEvents(sessionId) {
      return {
        session_id: sessionId,
        events: [{ ...event(1), session_id: "ses_other" }],
        head_seq: 1,
        next_after_seq: 1,
        has_more: false,
      };
    },
  };

  await assert.rejects(readSessionHistory(client, "ses_expected"), /invalid event/);
});

test("session history rejects sequence gaps instead of presenting incomplete state", async () => {
  const client = {
    async getSessionEvents(sessionId) {
      return {
        session_id: sessionId,
        events: [event(2)],
        head_seq: 2,
        next_after_seq: 2,
        has_more: false,
      };
    },
  };

  await assert.rejects(readSessionHistory(client, "ses_long"), /invalid event/);
});
