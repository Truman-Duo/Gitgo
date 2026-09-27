import type { StreamEvent } from "../daemon/streamEvents.js";
import { reduceStreamEvent, type StreamState } from "../daemon/streamReducer.js";

const PRIVATE_CHILD_EVENTS = new Set([
  "reasoning_delta",
  "text_delta",
  "provider_request_started",
  "stream_recovery",
]);

/**
 * Project one task-tree event into A's human-visible timeline.
 *
 * A subprocess's full stream remains available in its own view. The main
 * process receives semantic lifecycle/tool evidence (including diffs), but
 * never the subprocess's raw reasoning or
 * response draft. This is a UI projection only; it does not enter A's model
 * context and therefore does not create a second communication channel.
 */
export function reduceSupervisorEvent(
  state: StreamState,
  event: StreamEvent,
  rootProcessId: string,
): StreamState {
  const processId = String(event.process_id || "");
  if (!processId || !rootProcessId || processId === rootProcessId) {
    return reduceStreamEvent(state, event);
  }
  if (PRIVATE_CHILD_EVENTS.has(event.event)) return state;
  const projected = {
    ...event,
    actor_label: "Subprocess",
  } as StreamEvent;
  return reduceStreamEvent(state, projected, {preservePendingText: true});
}
