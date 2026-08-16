// src/mock/index.ts — unified re-export of the public MOCK_* surface.
// Consumers import mock data from here; individual constants live in domain files.
export { MOCK_PROJECTS, MOCK_PROJECT_ROWS } from "./projects.js";
export { MOCK_PROCESSES, MOCK_PROCESSES_COMPLETED } from "./processes.js";
export { MOCK_TOOL_EVENTS, MOCK_TOOL_EVENTS_COMPLETED } from "./toolEvents.js";
export { MOCK_PROVIDERS, MOCK_LLM_PROVIDERS, MOCK_PROVIDERS_COMPLETED } from "./providers.js";
export { MOCK_LESSONS } from "./lessons.js";
export { MOCK_CONTRACT, MOCK_CONTRACT_COMPLETED } from "./contract.js";
export { MOCK_STATUS, MOCK_STATUS_COMPLETED } from "./status.js";
export {
  MOCK_MAIN_CONVERSATION,
  MOCK_AGENT_CONVERSATIONS,
  MOCK_MAIN_CONVERSATION_COMPLETED,
  MOCK_AGENT_CONVERSATIONS_COMPLETED,
} from "./conversations.js";
export { MCP_MOCK_MAP } from "./registry.js";
