import {expect, test} from "bun:test";
import {runtimeNoticeFromEvent} from "./runtimeNotices.js";
for (const status of ["failed", "cancelled", "timed_out", "awaiting_user", "degraded", "unknown"]) {
  test("execution ending is not success: " + status, () => {
    const notice = runtimeNoticeFromEvent({event: "agent_complete", actor_kind: "worker", outcome: {status}} as any);
    expect(notice?.severity).not.toBe("success");
    expect(notice?.label).not.toContain("Main process");
  });
}
test("only recorded completion receives success", () => {
  const notice = runtimeNoticeFromEvent({event: "agent_complete", actor_kind: "supervisor", outcome: {status: "completed"}} as any);
  expect(notice?.severity).toBe("success");
  expect(notice?.label).toBe("Main process completed");
});
