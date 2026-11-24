import type {BackendClient} from "./client.js";

export type PublishNode = {id: string; status: string; reason: string};
export type PublishCheck = {id?: string; target: string; seeds: number[]; timeout: number};
export type PublishCheckResult = {check: PublishCheck; passed?: boolean; state?: string; error?: string;
  seed_results?: {seed: number; exit_code: number; duration_ms: number; output_tail: string}[]};
export type PublishPlatformTarget = {kind: string; instance: string; repository: string; account: string};
export type ReleasePhase = "prepared" | "draft" | "published" | "verified";
export type PublishReleaseJob = {id: string; state: string; through: ReleasePhase; observe_only: boolean;
  created_at: string; updated_at?: string; reason?: string; pending_effect?: string | null; remote_state?: string; release_url?: string;feed_state?:string;
  started_at?:string;finished_at?:string;queue_seconds?:number;elapsed_seconds?:number;
  transfer?: {asset: string; direction: "upload" | "download"; bytes: number; total: number | null; seconds: number; state: string};
  record_count: number; phases: Partial<Record<ReleasePhase, {state: string; seconds?: number; reason?: string}>>};
export type PublishReleaseControl = {schema_version: number; plan: {id: string; binding: string; package_id: string; version: string;
  target: PublishPlatformTarget; supported_phases: ReleasePhase[]; capabilities: Record<string, boolean>}; job?: PublishReleaseJob;
  history: Pick<PublishReleaseJob, "id" | "state" | "through" | "observe_only" | "created_at">[]};
export type PublishReleaseEvent = {sequence: number; name: string; state: string; time: number;
  reason?: string; code?: string; error?: string; seconds?: number; completion_unconfirmed?: boolean;
  result?:{timing?:{queue_seconds:number|null;runner_occupied_seconds:number|null;observed_job_span_seconds:number|null}}};
export type PublishReleaseStatus = {item_id: string; revision: number; recorded_only: true; stale: boolean;
  control: PublishReleaseControl; plan: {target: PublishPlatformTarget; body: string; recipe: {id: string; version: number};
    supported_phases: ReleasePhase[]; capabilities: Record<string, boolean>; source_oid?: string; tree_oid?: string; notes?: string;
    ci_artifacts?: PublishCIArtifact[];delivery_recipe?:string;
    native?:{channel:string;sequence:number;targets:string[];signing_key_id:string;upgrade_route:string}};
  events: PublishReleaseEvent[]; next_sequence: number; has_more: boolean};
export type PublishCIArtifact = {run_id: number; attempt: number; artifact_id: number; name: string; size: number; sha256: string;
  source_oid: string; workflow: string; workflow_id: number; repository_id: number; account_id: number; created_at: string;
  started_at: string; status: string; conclusion: string};
export type PublishCISources = {item_id: string; plan_binding: string; source_oid: string; remote_writes: false;
  rows: PublishCIArtifact[]; page: number; has_more: boolean};

export async function publishReleaseSources(client: BackendClient, project: string, itemId: string, planBinding: string, page = 1): Promise<PublishCISources> {
  const value = await client.callTool("publish.release.sources", {project, item_id:itemId, plan_binding:planBinding, page}) as PublishCISources;
  if (value.item_id !== itemId || value.plan_binding !== planBinding || value.page !== page || !Array.isArray(value.rows)
      || value.remote_writes !== false) throw new Error("PUBLISH_RESPONSE_MISMATCH");
  return value;
}
export type PlatformRecord = {id: string; title?: string; body?: string; state?: string; url?: string; author?: {id: string; login: string};
  number?: number; created_at?: string; anchor?: {path?: string; line?: number; new_path?: string; new_line?: number}; refs?: {head_sha?: string; base_sha?: string}};
export type PublishPlatformSnapshot = {id: string; binding: string; target: PublishPlatformTarget; resource: {kind: string; number?: number};
  page: number; query?: string; document?: PlatformRecord; documents?: PlatformRecord[]; comments?: PlatformRecord[]; reviews?: PlatformRecord[]; anchors?: PlatformRecord[];
  files?: {path: string; old_path: string; patch: string; unavailable: boolean}[];
  more?: boolean; comments_more?: boolean; reviews_more?: boolean; anchors_more?: boolean; files_more?: boolean};
export type PublishSharedPage = {schema_version: 1; item_id: string; observation_id: string; snapshot_id: string; remote: string;
  observed_oid: string; total: number; next_cursor: string | null; read_only: true;
  items: {id: string; title: string; owner: string; rework: string; result_oids: string[];
    reviews: {id: string; decision: string; note: string; owner: string}[];
    task_links?: {id: string; task_id: string; linked_task_id?: string; parent_task_id?: string; note?: string}[];
    branch_retention?: {id: string; ref: string; oid: string; owner: string; reason: string}[]}[]};
export type PublishItem = {
  id: string; identity_kind: "snapshot_observation" | "stable_item"; title: string; label: string;
  kind: "legacy_formal" | "candidate"; scope: "local"; owner: string | null;
  candidate_oid: string | null; target: string | null; status: string; current_node: string;
  reason: string; allowed_actions: string[]; nodes: PublishNode[];
  recorded: {synced: boolean; pushed: boolean; is_incoming: boolean; sources_cleared: boolean};
  source: {state_ref: string | null; legacy_index?: number; created_at: string};
  revision?: number; binding?: string; rework?: string; formal_eligible?: boolean;
  candidate?: {role: string; source_ref: string; oid: string; base_oid: string; tree_oid: string};
  plan?: {destination?: string; target_role?: string; target_absent?: boolean; method: string; repository: string; target_ref: string; expected_target_oid?: string;
    tree_oid?: string; commit_message?: string; test_target?: string; checks?: PublishCheck[]; requested_checks?: PublishCheck[];
    contributions?: {role: string; source_ref: string; oid: string; base_oid: string}[];
    selected_commits?: string[]; paths?: string[]; conflict_paths?: string[]; conflict_preview_tree?: string;
    commit_series?: {source_oid: string; tree_oid: string; message: string; number: number}[];
    excluded_path_fingerprints?: string[]; plugins?: {id: string; digest: string}[];
    dependencies?: {path: string; sha256: string}[]; transformed_files?: {path: string; before: string; after: string}[]};
  evidence?: {id: string; binding: string; kind: string; outcome: string; summary: string; scope: string; current?: boolean;
    checks?: PublishCheckResult[]; detail?: string;
    environment?: {digest: string; platform: string; file_count: number; packages: {name: string; version: string}[]}}[];
  operations?: {id: string; state: string; result_oid?: string; attribution?: string}[];
  run?: {id: string; state: string; steps: string[]; next: number; stop_after: string; reason?: string;
    actor: string; task_id: string; current?: {operation: string} | null;
    observations: {operation: string; effect_state: string; error?: string}[]};
  reviews?: {id: string; binding: string; decision: string; note: string; owner: string; actor: string; task_id: string}[];
  legacy_source?: {backup_ref: string; attribution: string; record: {prefix: string; number: number}};
  sharing?: {state: string; binding: string; result_oid: string; sha256: string; observation: {remote: string; ref: string; oid: string | null};
    preview: {title: string; owner: string; rework: string}};
  shared_observations?: {id: string; remote: string; items: number; recorded_at: string}[];
  collaboration?: {snapshots: PublishPlatformSnapshot[]; drafts: {id: string; binding: string; state: string; reason?: string;
    target: PublishPlatformTarget; resource: {kind: string; number?: number}; content: {kind: string; body: string; title?: string; state?: string;
      reference?: {kind: string; id: string}}; result?: PlatformRecord}[];
    followups?: {id: string; binding: string; target: PublishPlatformTarget; resource: {kind: string; number?: number};
      reference: {kind: string; id: string}; status: string; owner: string; note: string; recorded_at: string}[]};
  plugin_completion?: {state: string; git_result_oid: string; reason?: string};
  packages?: {id: string; version: string; source_oid: string; tree_oid: string; sha256: string; filename: string;
    assets: {name: string; sha256: string; size: number}[]; build?: {entry?: string}}[];
  distribution_operation?: {id: string; kind: string; state: string; reason?: string; destination?: string; package_id?: string;
    build_log?: string; build_environment?: {platform: string; architecture?: string; digest: string}};
  release_controller?: PublishReleaseControl;
  package_imports?: {id: string; sha256: string; source_oid: string; version: string; verification: string; installation: string}[];
  task_links?: {id: string; process_id: string; task_id: string; linked_task_id?: string; parent_process_id?: string; parent_task_id?: string; note?: string}[];
  branches?: {retention: {id: string; kind: string; ref: string; oid: string; owner: string; reason: string}[];
    plan?: {state: string; kind: string; binding: string; branch: string; expected_oid: string; archive_ref?: string;
      observation?: {remote: string; ref: string; oid: string | null}}};
};
export type PublishSnapshot = {
  schema_version: 1; project: string; snapshot_id: string; source: "governance_snapshot";
  scope: "local"; items: PublishItem[]; next_cursor: string | null; total: number;
};

export async function publishItems(client: BackendClient, project: string, cursor = ""): Promise<PublishSnapshot> {
  const value = await client.callTool("publish.items", {project, limit: 50, cursor}) as PublishSnapshot;
  if (value?.schema_version !== 1 || value.project !== project || !Array.isArray(value.items)
      || typeof value.snapshot_id !== "string") throw new Error("PUBLISH_RESPONSE_MISMATCH");
  return value;
}

export async function publishItem(client: BackendClient, project: string, itemId: string): Promise<PublishItem> {
  const value = await client.callTool("publish.item", {project, item_id: itemId}) as {item: PublishItem};
  if (value?.item?.id !== itemId || !Array.isArray(value.item.allowed_actions)) throw new Error("PUBLISH_RESPONSE_MISMATCH");
  return value.item;
}

export async function publishReleaseStatus(client: BackendClient, project: string, itemId: string,
  jobId = "", afterSequence = 0): Promise<PublishReleaseStatus> {
  const value = await client.callTool("publish.release.status", {project, item_id: itemId, job_id: jobId,
    after_sequence: afterSequence, limit: 25}) as PublishReleaseStatus;
  if (value?.item_id !== itemId || value.recorded_only !== true || !value.control?.plan || !Array.isArray(value.events)
      || !Number.isInteger(value.next_sequence) || value.next_sequence < afterSequence
      || jobId && value.control.job?.id !== jobId) throw new Error("PUBLISH_RESPONSE_MISMATCH");
  return value;
}

export type PublishCommandResult = {item?: PublishItem; error?: string; reason?: string; status?: string;
  operation_id: string; request_id: string; effect_state: string};

export async function publishCommand(client: BackendClient, project: string, operation: string,
  args: Record<string, unknown>, requestId: string): Promise<PublishCommandResult> {
  const value = await client.callTool(`publish.${operation}`, {project, ...args, request_id: requestId}, 360) as PublishCommandResult;
  if (!value || value.request_id !== requestId || typeof value.effect_state !== "string") throw new Error("PUBLISH_RESPONSE_MISMATCH");
  if (value.error) throw Object.assign(new Error(value.error), {code: value.error, effect_state: value.effect_state});
  return value;
}
