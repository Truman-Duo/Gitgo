// src/mock/projects.ts — mock project data (raw list + enriched rows + archived).
import type { ProjectInfo, ProjectRow } from "../hooks/useGitgoData.js";

// ── ProjectInfo (raw list from gitgo_list_projects) ──────────

export const MOCK_PROJECTS: ProjectInfo[] = [
  {
    name: "ergo",
    workspace: "/home/gitgo/ergo",
    backup: "/mnt/backup/ergo",
    commit_prefix: "ERGO",
  },
  {
    name: "shard",
    workspace: "/home/gitgo/shard",
    backup: "/mnt/backup/shard",
    commit_prefix: "SHARD",
  },
  {
    name: "atlas",
    workspace: "/home/gitgo/atlas",
    backup: "/mnt/backup/atlas",
    commit_prefix: "ATLAS",
  },
  {
    name: "forge",
    workspace: "/home/gitgo/forge",
    backup: "/mnt/backup/forge",
    commit_prefix: "FORGE",
  },
  {
    name: "nexus",
    workspace: "/home/gitgo/nexus",
    backup: "/mnt/backup/nexus",
    commit_prefix: "NEXUS",
  },
];

// ── ProjectRow (enriched, for Overview) ──────────────────────

export const MOCK_PROJECT_ROWS: ProjectRow[] = [
  {
    ...MOCK_PROJECTS[0],
    pendingLessons: 3,
    features: 12,
    constraints: 5,
    techStack: "Python, FastAPI, PostgreSQL",
    daemonOnline: true,
    activeProcessCount: 2,
    governanceStatus: "正常",
    llmProviderSummary: "3 providers",
    llmStatus: "ok",
    lessonsSeverity: "med",
  },
  {
    ...MOCK_PROJECTS[1],
    pendingLessons: 7,
    features: 8,
    constraints: 3,
    techStack: "Rust, Actix, SQLite",
    daemonOnline: true,
    activeProcessCount: 4,
    governanceStatus: "熔断",
    llmProviderSummary: "2 providers",
    llmStatus: "ok",
    lessonsSeverity: "high",
  },
  {
    ...MOCK_PROJECTS[2],
    pendingLessons: 0,
    features: 9,
    constraints: 4,
    techStack: "Go, gRPC, CockroachDB",
    daemonOnline: true,
    activeProcessCount: 0,
    governanceStatus: "正常",
    llmProviderSummary: "3 providers",
    llmStatus: "ok",
    lessonsSeverity: "none",
  },
  {
    ...MOCK_PROJECTS[3],
    pendingLessons: 0,
    features: 6,
    constraints: 2,
    techStack: "Rust, Tokio, PostgreSQL",
    daemonOnline: true,
    activeProcessCount: 0,
    governanceStatus: "正常",
    llmProviderSummary: "2 providers",
    llmStatus: "ok",
    lessonsSeverity: "none",
  },
  {
    ...MOCK_PROJECTS[4],
    pendingLessons: 0,
    features: 3,
    constraints: 1,
    techStack: "TypeScript, Bun, SQLite",
    daemonOnline: false,
    activeProcessCount: 0,
    governanceStatus: "?",
    llmProviderSummary: "未配置",
    llmStatus: "error",
    lessonsSeverity: "none",
  },
];

// ── Archived projects (for /archive list) ────────────────────

export const MOCK_ARCHIVED = [
  {
    name: "legacy-api",
    workspace: "/archive/legacy-api",
    release_url: "https://git.example.com/legacy-api",
    pending_hard_delete_at: "",
  },
  {
    name: "proto-2025",
    workspace: "/archive/proto-2025",
    release_url: "https://git.example.com/proto-2025",
    pending_hard_delete_at: "2026-08-20T00:00:00Z",
  },
];
