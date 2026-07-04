// src/hooks/useChat.ts — Chat message state + send to A agent
import { useState, useCallback } from "react";
import type { McpClient } from "../mcp/client.js";
import type { ChatMessage } from "../components/ChatPanel.js";

export function useChat(client: McpClient, project: string) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);

  const send = useCallback(async (text: string) => {
    if (!text.trim()) return;
    const userMsg: ChatMessage = {
      role: "user", content: text,
      timestamp: new Date().toISOString(),
    };
    // Unsent bubble: show pending immediately (DeepSeek pattern)
    const pendingMsg: ChatMessage = {
      role: "assistant", content: "...",
      timestamp: new Date().toISOString(), pending: true,
    };
    setMessages((prev) => [...prev, userMsg, pendingMsg]);

    try {
      const result: any = await client.callTool("gitgo_agent_chat", {
        project, message: text,
      });
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
