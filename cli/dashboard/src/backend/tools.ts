// Typed wrappers for transport-neutral native application operations.
import type { BackendClient } from "./client.js";

export const listProjects = (c: BackendClient) => c.callTool("project.list");
// The Host's internal aggregation deadline remains 1s.  This outer allowance
// also covers cold Python scheduling and IPC on a busy Windows machine; it is
// not permission for any individual project to block the list.
export const projectOverview = (c: BackendClient) => c.callTool("project.overview", {}, 10);
export const createProject = (c: BackendClient, p: {
  name: string;
  workspace_path: string;
  workspace_mode: "attach_existing" | "create_new";
  release_url?: string;
  llm_provider?: string;
}) => c.callTool("project.create", p);
export const publishGet = (c: BackendClient, project: string) => c.callTool("publish.get", {project});
export const publishSet = (c: BackendClient, project: string, section: string, value: unknown) =>
  c.callTool("publish.set", {project, section, value});
export const listArchivedProjects = (c: BackendClient) => c.callTool("project.list_archived");
export const archiveProject = (c: BackendClient, name: string) => c.callTool("project.archive", { name });
export const deleteProject = (c: BackendClient, name: string, mode: "soft" | "hard") => c.callTool("project.delete", { name, mode });
export const cancelPendingDelete = (c: BackendClient, name: string) => c.callTool("project.cancel_delete", { name });

export const lessonList = (c: BackendClient, project: string) => c.callTool("lesson.list", { project });
export const lessonSearch = (c: BackendClient, project: string, query: string) => c.callTool("lesson.search", { project, query });
export const lessonVerify = (c: BackendClient, project: string, lessonId: string) => c.callTool("lesson.verify", { project, lesson_id: lessonId });
export const lessonHarvestPreview = (c: BackendClient, project: string, observation: string) =>
  c.callTool("lesson.harvest.preview", { project, observation });
export const lessonHarvestResolve = (
  c: BackendClient, project: string, proposalId: string, action: "accept" | "discard",
) => c.callTool("lesson.harvest.resolve", { project, proposal_id: proposalId, action });
export const contractShow = (c: BackendClient, project: string) => c.callTool("contract.show", { project });
export const governanceQuality = (c: BackendClient, project: string) => c.callTool("governance.quality", { project });
export const governancePatterns = (c: BackendClient, project: string) => c.callTool("governance.patterns", { project });
export const governanceFeed = (
  c: BackendClient, project: string, limit = 20, cursor = "",
) => c.callTool("governance.feed", { project, limit, cursor });
export const governanceReleases = (c: BackendClient, project: string) => c.callTool("governance.releases", { project });
export const memorySnapshot = (c: BackendClient, project: string) => c.callTool("memory.snapshot", { project });
export const memoryList = (c: BackendClient, project: string) => c.callTool("memory.list", { project });
export const memoryRestore = (c: BackendClient, project: string, ts: string) => c.callTool("memory.restore", { project, ts });
export const historyList = (c: BackendClient, project: string, limit = 20) => c.callTool("history.list", { project, limit });
export const customToolList = (c: BackendClient, project: string, includeArchived = true) =>
  c.callTool("runtime.tools.list", { project, include_archived: includeArchived });
export const customToolArchive = (c: BackendClient, project: string, name: string) =>
  c.callTool("runtime.tools.archive", { project, name });
export const customToolRestore = (c: BackendClient, project: string, name: string) =>
  c.callTool("runtime.tools.restore", { project, name });
export const trialList = (c: BackendClient, project: string) => c.callTool("trial.list", { project });
export const trialTriage = (c: BackendClient, project: string, index: number, action: string) => c.callTool("trial.triage", { project, index, action });
export const formalList = (c: BackendClient, project: string) => c.callTool("formal.list", { project });
export const formalEditMessage = (c: BackendClient, project: string, index: number, message: string) => c.callTool("formal.edit_message", { project, index, message });
export const formalDelete = (c: BackendClient, project: string, index: number) => c.callTool("formal.delete", { project, index });
export const formalDissolve = (c: BackendClient, project: string, index: number) => c.callTool("formal.dissolve", { project, index });
export const loopStatus = (c: BackendClient, project: string) => c.callTool("runtime.status", { project });
export const runtimeSummaries = (c: BackendClient) => c.callTool("runtime.summaries");
export const runtimeUsage = (
  c: BackendClient, project = "", limit = 100, cursor = "",
) => c.callTool("runtime.usage", { project, limit, cursor });
export const runtimeTrace = (
  c: BackendClient, project: string, args: Record<string, any>,
) => c.callTool("runtime.trace", { project, ...args });
export const manualCompact = (
  c: BackendClient, project: string, processId = "",
  decision?: { decision_id: string; choice: "force_compact" | "stop" },
) => c.callTool("runtime.compact", { project, process_id: processId, ...decision }, 140);
export const previewUndo = (
  c: BackendClient, project: string, processId = "",
) => c.callTool("runtime.undo.preview", {project, process_id: processId}, 35);
export const commitUndo = (
  c: BackendClient, project: string, processId: string, checkpointId: string,
) => c.callTool("runtime.undo", {
  project, process_id: processId, checkpoint_id: checkpointId,
}, 35);
export const btwAsk = (
  c: BackendClient, project: string, question: string,
  sidecarId = "", history: Array<{ role: "user" | "assistant"; content: string }> = [],
  processIds: string[] = [], onEvent?: (event: Record<string, any>) => void,
) => c.callTool("runtime.btw", {
  project, question, sidecar_id: sidecarId, history,
  process_ids: processIds, process_id: processIds[0] || "",
}, 140, onEvent);
export const cancelBtw = (
  c: BackendClient, project: string, sidecarId: string,
) => c.callTool("runtime.btw.cancel", { project, sidecar_id: sidecarId }, 15);
export const saveBtwNote = (
  c: BackendClient, project: string, sidecarId: string, note: string,
) => c.callTool("runtime.btw.note", { project, sidecar_id: sidecarId, note });
export const sendRuntimeFeedback = (
  c: BackendClient, project: string, processId: string, message: string,
  onEvent?: (event: Record<string, any>) => void,
) => c.callTool("runtime.feedback", { project, process_id: processId, message }, 330, onEvent);
export const agentChat = (c: BackendClient, project: string, message: string) => c.callTool("runtime.chat", { project, message }, 330);
export const stopProcess = (c: BackendClient, project: string, processId: string) => c.callTool("runtime.stop", { project, process_id: processId });
export const cancelRequest = (c: BackendClient, requestId: string) =>
  c.callTool("host.cancel", { request_id: requestId }, 15);
export const resumeRecovery = (
  c: BackendClient, project: string, processId: string,
  options: { manuallyVerified?: boolean; verificationNote?: string } = {},
) => c.callTool("runtime.recovery.resume", {
  project,
  process_id: processId,
  manually_verified: options.manuallyVerified || false,
  verification_note: options.verificationNote || "",
}, 330);
export const discardRecovery = (
  c: BackendClient, project: string, processId: string, reason = "",
) => c.callTool("runtime.recovery.discard", { project, process_id: processId, reason });
export const configGet = (c: BackendClient) => c.callTool("config.get");
export const detectTerminals = (c: BackendClient) => c.callTool("config.terminals", {}, 15);
export const configSet = (c: BackendClient, key: string, value: any) => c.callTool("config.set", { key, value });
export const testWebSearchConfig = (c: BackendClient) => c.callTool("config.web_search.test", {}, 35);
export const templateList = (c: BackendClient) => c.callTool("template.list");
export const templateAdd = (c: BackendClient, p: { name: string; description: string; header_format?: string; body_format?: string }) => c.callTool("template.add", p);
export const templateEdit = (c: BackendClient, name: string, description: string) => c.callTool("template.edit", { name, description });
export const templateDelete = (c: BackendClient, name: string) => c.callTool("template.delete", { name });
export const llmStatus = (c: BackendClient) => c.callTool("provider.status");
export const llmSave = (c: BackendClient, p: { id?: string; name: string; base_url: string; api_key: string; model_id: string; protocol: string; context_window: number; max_output_tokens: number; retain_api_key?: boolean }) => c.callTool("provider.save", { provider_id: p.id || "", name: p.name, base_url: p.base_url, api_key: p.api_key, model_id: p.model_id, protocol: p.protocol, context_window: p.context_window, max_output_tokens: p.max_output_tokens, retain_api_key: p.retain_api_key || false });
export const llmSwitch = (c: BackendClient, providerId: string) => c.callTool("provider.switch", { provider_id: providerId });
export const llmDelete = (c: BackendClient, providerId: string) => c.callTool("provider.delete", { provider_id: providerId });
export const llmTest = (c: BackendClient, providerId: string) => c.callTool("provider.test", { provider_id: providerId }, 70);
export const exportData = (
  c: BackendClient, project: string,
  options: { minimal: boolean; output_path: string; output_format: "json" | "yaml" | "markdown" },
) => c.callTool("project.export", {
  project,
  minimal: options.minimal,
  include_identity: !options.minimal,
  output_path: options.output_path,
  output_format: options.output_format,
});
