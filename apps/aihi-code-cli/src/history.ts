import type { EventRecord, SessionEventsResult } from "@aihi/code-protocol";
import type { RpcClient } from "./rpc/client.js";

const HISTORY_PAGE_SIZE = 500;
type HistoryClient = Pick<RpcClient, "getSessionEvents">;

export interface SessionHistory {
  events: EventRecord[];
  headSeq: number;
}

/** Replay every durable page and reject a malformed cursor before it can loop forever. */
export async function readSessionHistory(
  client: HistoryClient,
  sessionId: string,
): Promise<SessionHistory> {
  const events: EventRecord[] = [];
  let afterSeq = 0;
  let headSeq = 0;
  let stalledTailReads = 0;
  while (true) {
    const page: SessionEventsResult = await client.getSessionEvents(
      sessionId,
      afterSeq,
      HISTORY_PAGE_SIZE,
    );
    if (page.session_id !== sessionId) {
      throw new Error(`Session history returned another session: ${page.session_id}`);
    }
    if (page.head_seq < headSeq) {
      throw new Error("Session history head moved backwards");
    }
    let previousSeq = afterSeq;
    for (const event of page.events) {
      if (
        event.session_id !== sessionId ||
        event.seq === null ||
        event.seq !== previousSeq + 1
      ) {
        throw new Error(`Session history contains an invalid event after seq ${previousSeq}`);
      }
      previousSeq = event.seq;
    }
    if (page.next_after_seq !== previousSeq) {
      throw new Error("Session history cursor does not match the last event");
    }
    if (page.head_seq < previousSeq) {
      throw new Error("Session history head is behind the returned events");
    }
    events.push(...page.events);
    afterSeq = page.next_after_seq;
    headSeq = Math.max(headSeq, page.head_seq);
    const tailMovedWhileReading = afterSeq < page.head_seq;
    if (!page.has_more && !tailMovedWhileReading) break;
    if (page.events.length === 0) {
      // The Worker reads events and the current head separately. One empty
      // retry is legitimate when an event commits between those reads; a
      // repeated stall is a broken peer and must not spin forever.
      if (!page.has_more && tailMovedWhileReading && stalledTailReads === 0) {
        stalledTailReads += 1;
        continue;
      }
      throw new Error(`Session history cursor did not advance after seq ${afterSeq}`);
    }
    stalledTailReads = 0;
  }
  return { events, headSeq };
}
