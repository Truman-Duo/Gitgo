// src/mock/contract.ts — mock contract summary data (running + completed scenarios).
export const MOCK_CONTRACT = {
  decided_features: [
    "auth-module",
    "api-gateway",
    "event-bus",
    "scheduler",
    "cache-layer",
    "db-migrations",
    "logging-pipeline",
    "metrics-dashboard",
    "rate-limiter",
    "circuit-breaker",
    "config-hot-reload",
    "health-check-endpoint",
  ],
  architecture_constraints: [
    "no circular deps",
    "max 3 service layers",
    "async I/O only",
    "stateless services",
    "structured logging required",
  ],
  tech_stack: ["Python", "FastAPI", "PostgreSQL"],
  updated_at: "2026-08-12T18:30:00Z",
};

export const MOCK_CONTRACT_COMPLETED = {
  decided_features: [
    "rate-limiter",
    "request-throttle",
    "token-bucket",
    "quota-reset",
    "burst-control",
    "metrics-export",
  ],
  architecture_constraints: [
    "no shared mutable state",
    "async I/O only",
    "idempotent handlers",
  ],
  tech_stack: ["Go", "gRPC", "CockroachDB"],
  updated_at: "2026-08-10T12:00:00Z",
};
