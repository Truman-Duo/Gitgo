import type {ChatMessage, StreamingRow} from "../types.js";

const terminal = new Set(["completed","failed","cancelled","timed_out","degraded"]);
export function isTerminalRound(message: ChatMessage): boolean {
  return message.role === "assistant" && !message.decision &&
    (terminal.has(message.status || "") || message.kind === "outcome" || message.kind === "error");
}
function isAnswer(message: ChatMessage): boolean {
  return message.role === "assistant" && !message.decision && message.kind !== "progress" &&
    message.status !== "awaiting_user";
}

/** A read-only view of the journal: one answer owns all of its process records.
 * Original messages, task identities and decision authority are never changed. */
export function projectConversationRounds(messages: ChatMessage[], streaming: StreamingRow | null): ChatMessage[] {
  let interval = "history";
  const scoped = messages.map((message,index) => {
    if (message.role === "user") interval = message.turn_id || message.message_id || message.id || `user:${index}`;
    const scope = message.kind === "knowledge_notice" ? interval
      : message.decision?.task_id || message.turn_id || (interval === "history"
        ? message.message_id || message.id || `unscoped:${index}` : interval);
    return {message,scope};
  });
  const owners = new Map<string, ChatMessage>();
  const rank=(message:ChatMessage)=>message.kind === "outcome" || message.message_id?.startsWith("outcome:") ? 2 : isTerminalRound(message) ? 1 : 0;
  for (const {message,scope} of scoped) if (isAnswer(message)) {
    const previous=owners.get(scope);
    if(!previous || rank(message)>=rank(previous))owners.set(scope,message);
  }
  const records = new Map<ChatMessage, ChatMessage[]>();
  const seen = new Map<ChatMessage, Set<string>>();
  const result: ChatMessage[] = [];
  const summaries=new Map<string,number>();
  for (const {message,scope} of scoped) {
    const owner = owners.get(scope);
    if (owner && owner !== message && message.role !== "user") {
      if ((message.message_id && message.message_id===owner.message_id) ||
          message.awaiting_persistence && isAnswer(message) && rank(owner)===2) continue;
      const list = records.get(owner) || [];
      const identities = seen.get(owner) || new Set<string>();
      const identity = message.message_id || message.id || (message.decision ? `decision:${message.decision.decision_id}`
        : message.update_id ? `progress:${message.process_id || "host"}:${message.update_id}` : "");
      const previousDecision=message.decision ? list.findIndex(item=>item.decision
        && item.decision.task_id===message.decision!.task_id && item.decision.process_id===message.decision!.process_id
        && item.decision.decision_id===message.decision!.decision_id) : -1;
      if(previousDecision>=0){
        if(message.decision_answer?.trim() || !list[previousDecision]!.decision_answer)list[previousDecision]=message;
      }else if (!identity || !identities.has(identity)) list.push(message);
      if (identity) identities.add(identity);
      records.set(owner,list); seen.set(owner,identities);
      if(message.decision && message.decision_answer?.trim()){
        const key=JSON.stringify([message.decision.task_id,message.decision.process_id,message.decision.decision_id]);
        const summary={...message,kind:"decision_summary"};
        const position=summaries.get(key);
        if(position===undefined){summaries.set(key,result.length);result.push(summary);}
        else result[position]=summary;
      }
      continue;
    }
    result.push(message);
  }
  // Streaming remains the owner of its live activity; no independent public
  // progress row is synthesized during a stream or after it settles.
  const liveKeys = new Set((streaming?.activity || []).flatMap(item => item.kind === "progress" && item.update_id
    ? [`${item.turn_id || interval}:${item.process_id || "host"}:${item.update_id}`] : []));
  return result.filter(message => !streaming || message.kind !== "progress" || !message.update_id ||
    !liveKeys.has(`${message.turn_id || interval}:${message.process_id || "host"}:${message.update_id}`))
    .map(message => records.has(message) ? {...message,process_messages:records.get(message)} : message);
}
