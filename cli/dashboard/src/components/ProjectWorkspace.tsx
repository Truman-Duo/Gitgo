// src/components/ProjectWorkspace.tsx — Scene 2: split layout with Chat + Context
// Borrowed from Claude Code FullscreenLayout slot pattern
import React, { memo, useState, useEffect, useMemo } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import { useLoopData } from "../hooks/useLoopData.js";
import { useChat } from "../hooks/useChat.js";
import { ChatPanel } from "./ChatPanel.js";
import { ContextPanel } from "./ContextPanel.js";

type Props = {
  project: string;
  client: McpClient;
  cols: number;
  rows: number;
  onBack: () => void;
  onEnterAgent: (processId: string) => void;
  refreshKey: number;
};

function buildGovernanceBrief(
  project: string,
  daemonOnline: boolean,
  processes: Record<string, any>,
  toolEvents: { role: string; tool_name: string; allowed: boolean }[],
): string {
  const procs = Object.values(processes);
  const tools = toolEvents.slice(-5);
  return (
    `项目: ${project}\n` +
    `Daemon: ${daemonOnline ? "在线" : "离线"}\n` +
    `活跃进程: ${procs.filter((p: any) => p.status === "running").length}\n` +
    `最近工具调用: ${tools.length} 条\n` +
    (tools.length > 0
      ? tools.map((t) => `  ${t.role}/${t.tool_name} ${t.allowed ? "OK" : "DENIED"}`).join("\n")
      : "  无")
  );
}

export const ProjectWorkspace = memo(function ProjectWorkspace({
  project, client, cols, rows, onBack, onEnterAgent, refreshKey,
}: Props) {
  const { processes, toolEvents, daemonOnline, loading } = useLoopData(client, project, 5);
  const { messages, send } = useChat(client, project);

  const [governanceBrief, setGovernanceBrief] = useState<string | null>(null);
  const [chatInput, setChatInput] = useState("");
  const [chatMode, setChatMode] = useState(false);
  const [contextFocus, setContextFocus] = useState(false);
  const [processSel, setProcessSel] = useState(0);

  useEffect(() => {
    if (!governanceBrief) {
      setGovernanceBrief(buildGovernanceBrief(project, daemonOnline, processes, toolEvents));
    }
  }, [processes, toolEvents, daemonOnline, project, governanceBrief]);

  const handleProcessSelect = (pid: string) => onEnterAgent(pid);

  useInput((input: string, key: any) => {
    if (chatMode) {
      if (key.return) {
        const text = chatInput.trim();
        setChatInput("");
        setChatMode(false);
        if (text) send(text);
        return;
      }
      if (key.escape) { setChatInput(""); setChatMode(false); return; }
      if (key.backspace) { setChatInput((p) => p.slice(0, -1)); return; }
      if (input && input.length >= 1 && !key.ctrl && !key.meta) {
        setChatInput((p) => p + input);
        return;
      }
      return;
    }

    if (key.tab) { setContextFocus((p) => !p); return; }

    if (contextFocus) {
      if (key.escape) { setContextFocus(false); return; }
      if (key.upArrow) { setProcessSel((p) => Math.max(0, p - 1)); return; }
      if (key.downArrow) {
        setProcessSel((p) => Math.min(Object.keys(processes).length - 1, p + 1));
        return;
      }
      if (key.return) {
        const pid = Object.keys(processes)[processSel];
        if (pid) handleProcessSelect(pid);
        return;
      }
      return;
    }

    if (key.escape || input === "q") { onBack(); return; }
    if (input === ":") { setChatMode(true); setChatInput(""); return; }
  });

  const chatW = Math.floor(cols * 0.65);
  const ctxW = cols - chatW - 2;

  return (
    <Box flexDirection="column" width={cols} paddingTop={1}>
      {loading ? (
        <Box paddingLeft={1}><Text dimColor>Loading loop data...</Text></Box>
      ) : null}

      <Box flexDirection="row" flexGrow={1}>
        <ChatPanel messages={messages} governanceBrief={governanceBrief} cols={chatW} />
        <ContextPanel
          project={project} client={client} cols={ctxW}
          processes={processes} toolEvents={toolEvents}
          processSel={processSel} onProcessSelect={handleProcessSelect}
          processFocus={contextFocus}
        />
      </Box>

      <Box flexDirection="column" marginTop={1}>
        <Box
          borderStyle="single"
          borderColor={chatMode ? "cyan" : "green"}
          paddingLeft={1} paddingRight={1}
        >
          {chatMode ? (
            <Box flexDirection="row">
              <Text color="cyan">Chat: </Text>
              <Text>{chatInput}</Text>
              <Text dimColor>█</Text>
            </Box>
          ) : (
            <Text dimColor>
              {contextFocus
                ? "Context ↑↓ select  Enter detail  Esc back"
                : ": chat  Tab context  Esc/q back"}
            </Text>
          )}
        </Box>
      </Box>
    </Box>
  );
});
