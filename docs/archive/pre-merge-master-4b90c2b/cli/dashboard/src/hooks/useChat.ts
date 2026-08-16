// src/hooks/useChat.ts — Chat message state + send to agent
import { useState, useCallback } from "react";
import type { McpClient } from "../mcp/client.js";
import { getDaemonClient } from "../clients.js";

export function useChat(client: McpClient, project: string) {
  const [messages, setMessages] = useState<{role:string,content:string,timestamp:string,pending?:boolean}[]>([]);

  const send = useCallback(async (text: string) => {
    if (!text.trim()) return;
    const userMsg = { role: "user" as const, content: text, timestamp: new Date().toISOString() };
    const pendingMsg = { role: "assistant" as const, content: "...", timestamp: new Date().toISOString(), pending: true };
    setMessages((prev) => [...prev, userMsg, pendingMsg]);

    try {
      // Prefer native daemon for agent_chat; fall back to MCP
      const daemon = getDaemonClient();
      let result: any;
      if (daemon?.ready) {
        result = await daemon.callTool("gitgo_agent_chat", { project, message: text });
      } else {
        result = await client.callTool("gitgo_agent_chat", { project, message: text });
      }

      setMessages((prev) => {
        const copy = [...prev];
        copy[copy.length - 1] = {
          role: "assistant",
          content: result?.response || "(无回复)",
          timestamp: new Date().toISOString(),
        };
        return copy;
      });
    } catch {
      setMessages((prev) => {
        const copy = [...prev];
        copy[copy.length - 1] = {
          role: "assistant",
          content: "[Error: 调用失败]",
          timestamp: new Date().toISOString(),
        };
        return copy;
      });
    }
  }, [client, project]);

  return { messages, send };
}
