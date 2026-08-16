// src/chat/sendChat.ts — transport orchestration for sending a chat message.
// Picks the active transport (mock / native daemon streaming / MCP fallback)
// and drives the transient streaming row + final assistant message via callbacks.
// `useChat` keeps only state and delegates the actual send to this module.
import type { McpClient } from "../mcp/client.js";
import type { StreamingRow, ToolCallCard } from "../types.js";
import type { ToolEvent } from "../hooks/useLoopData.js";
import { getDaemonClient } from "../clients.js";
import { agentChat, loopStatus } from "../mcp/tools.js";
import { MockMcpClient } from "../mock/MockMcpClient.js";
import { simulateMockStream } from "../mock/mockStream.js";
import { initStreamState, reduceStreamEvent, finalizeTools } from "../daemon/streamReducer.js";

export type ChatSendCallbacks = {
  onStream: (row: StreamingRow) => void;
  onDone: (content: string, tools?: ToolCallCard[]) => void;
  onError: (message: string) => void;
};

export async function sendChat(
  client: McpClient,
  project: string,
  text: string,
  startTime: string,
  cb: ChatSendCallbacks,
): Promise<void> {
  // --mock mode: simulate a token stream so the live dashboard is demoed.
  if ((client as unknown) instanceof MockMcpClient) {
    await simulateMockStream({ onStream: cb.onStream, onDone: cb.onDone });
    return;
  }

  // v0.45: prefer streaming native daemon; fall back to MCP.
  const daemon = getDaemonClient();
  if (daemon?.ready) {
    try {
      let state = initStreamState();
      await daemon.sendTaskStreaming(
        {
          cmd: "task",
          action: "chat",
          instruction: text,
          role: "executor",
          ring_level: 3,
          max_steps: 50,
          task_description: text.slice(0, 200),
        },
        {
          onChunk: (event) => {
            state = reduceStreamEvent(state, event);
            cb.onStream({ text: state.text || "...", tools: state.tools, timestamp: startTime });
          },
          onComplete: (result: any) => {
            const finalText = result?.result?.response || state.text || "(no reply)";
            cb.onDone(finalText, finalizeTools(state.tools));
          },
          onError: (err: Error) => {
            cb.onError(err.message);
          },
        },
      );
      return;
    } catch {
      // daemon streaming failed — fall through to MCP.
    }
  }

  // MCP fallback
  try {
    const result: any = await agentChat(client, project, text);
    const responseText = result?.response || "(no reply)";
    const processId = result?.process_id || "";

    let tools: ToolCallCard[] | undefined;
    try {
      const loopData: any = await loopStatus(client, project);
      const events: ToolEvent[] = (loopData?.recent_tool_executed || []) as ToolEvent[];
      tools = events
        .filter((e) => {
          if (processId && e.process_id !== processId) return false;
          if (e.timestamp < startTime) return false;
          return true;
        })
        .map((e) => ({
          tool_name: e.tool_name,
          target: "",
          allowed: e.allowed,
          status_label: e.allowed ? "OK" : "DENIED",
          duration_ms: e.duration_ms,
          timestamp: e.timestamp,
          blocked_reason: e.blocked_reason,
          diff: e.diff,
          state: e.allowed ? ("completed" as const) : ("error" as const),
        }));
      if (tools.length === 0) tools = undefined;
    } catch {
      // loop_status unavailable — set message without tools.
    }

    cb.onDone(responseText, tools);
  } catch {
    cb.onError("call failed");
  }
}
