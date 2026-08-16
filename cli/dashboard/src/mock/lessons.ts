// src/mock/lessons.ts — mock pending-lesson data.
export const MOCK_LESSONS = {
  pending: [
    {
      id: "lesson-abc123def456",
      severity: "high",
      trigger: "write_file blocked by security_policy in workspace",
      created_at: "2026-08-13T08:05:00Z",
    },
    {
      id: "lesson-789xyz012uvw",
      severity: "medium",
      trigger: "bash command exceeded timeout threshold",
      created_at: "2026-08-13T07:30:00Z",
    },
    {
      id: "lesson-crit001abc",
      severity: "critical",
      trigger: "identity_guard: process proc-005 killed after 3 consecutive denials",
      created_at: "2026-08-13T07:20:00Z",
    },
  ],
};
