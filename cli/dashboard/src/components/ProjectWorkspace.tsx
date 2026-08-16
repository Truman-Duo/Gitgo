// src/components/ProjectWorkspace.tsx — Scene 2: full-width chat (v4 blueprint aligned)
import React, { memo, useEffect } from "react";
import { Box, Text } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import type { ChatScrollHandle } from "../types.js";
import { useLoopData } from "../hooks/useLoopData.js";
import { useChat } from "../hooks/useChat.js";
import { ChatPanel } from "./ChatPanel.js";

type Props = {
  project: string;
  client: McpClient;
  cols: number;
  rows: number;
  onBack: () => void;
  onEnterAgent: (processId: string) => void;
  refreshKey: number;
  sendChatRef?: React.MutableRefObject<(text: string) => void>;
  scrollChatRef: React.MutableRefObject<ChatScrollHandle | null>;
};

export const ProjectWorkspace = memo(function ProjectWorkspace({
  project, client, cols, sendChatRef, scrollChatRef,
}: Props) {
  const { loading, mainConversation } = useLoopData(client, project, 2);
  const { messages, streaming, send } = useChat(client, project, mainConversation);

  useEffect(() => {
    if (sendChatRef) {
      sendChatRef.current = (text: string) => { send(text); };
    }
  }, [send, sendChatRef]);

  return (
    <Box flexDirection="column" width={cols} paddingTop={1} flexGrow={1}>
      {loading ? (
        <Box paddingLeft={1}><Text dimColor>Loading loop data...</Text></Box>
      ) : null}
      <ChatPanel messages={messages} streaming={streaming} cols={cols} project={project} scrollChatRef={scrollChatRef} />
    </Box>
  );
});
