// src/hooks/useChat.ts — Chat message state + send to agent.
// Keeps only state (messages + transient streaming row) and re-seeding; the
// actual transport selection + stream parsing lives in src/chat/sendChat.ts.
import { useState, useCallback, useEffect } from "react";
import type { McpClient } from "../mcp/client.js";
import type { ChatMessage, StreamingRow } from "../types.js";
import { sendChat } from "../chat/sendChat.js";

// Streaming text lives in a separate transient row (never inside `messages`),
// so the authoritative poll snapshot can wholesale-replace `messages` without
// clobbering an in-flight token stream. On complete (or error) the transient
// row is dissolved and the final assistant message is committed to `messages`.
export function useChat(client: McpClient, project: string, seed?: ChatMessage[] | null) {
  const [messages, setMessages] = useState<ChatMessage[]>(seed ?? []);
  const [streaming, setStreaming] = useState<StreamingRow | null>(null);

  // Re-seed when the injected conversation changes (e.g. switching B agent).
  useEffect(() => {
    setMessages(seed ?? []);
  }, [seed]);

  const send = useCallback(async (text: string) => {
    if (!text.trim()) return;
    const startTime = new Date().toISOString();
    setMessages((prev) => [...prev, { role: "user", content: text, timestamp: startTime, final: true }]);
    setStreaming({ text: "", tools: [], timestamp: startTime });

    await sendChat(client, project, text, startTime, {
      onStream: setStreaming,
      onDone: (content, tools) => {
        setMessages((prev) => [...prev, {
          role: "assistant",
          content,
          timestamp: new Date().toISOString(),
          tools: tools && tools.length > 0 ? tools : undefined,
          final: true,
        }]);
        setStreaming(null);
      },
      onError: (message) => {
        setMessages((prev) => [...prev, {
          role: "assistant",
          content: `[Error: ${message}]`,
          timestamp: new Date().toISOString(),
          final: true,
        }]);
        setStreaming(null);
      },
    });
  }, [client, project]);

  return { messages, streaming, send };
}
