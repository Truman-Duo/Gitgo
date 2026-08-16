// src/components/LLMConfigPanel.tsx — Ink terminal Provider management panel
// Patterns: cc-switch Provider list + switch, Ink keyboard nav, ProjectWorkspace useInput style
import React, { memo, useState, useEffect, useCallback } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import { useLLMConfig, type LLMProvider } from "../hooks/useLLMConfig.js";

type Props = {
  client: McpClient;
  project: string;
  cols: number;
  rows: number;
  onBack: () => void;
};

type Mode = "list" | "edit";

const FIELD_LABELS = ["名称", "Base URL", "API Key", "Model ID"];

export const LLMConfigPanel = memo(function LLMConfigPanel({
  client, project, cols, rows, onBack,
}: Props) {
  const {
    providers, activeProvider, loading,
    saveProvider, switchProvider, deleteProvider, fetchStatus,
  } = useLLMConfig(client, project);

  const [mode, setMode] = useState<Mode>("list");
  const [selIdx, setSelIdx] = useState(0);
  const [editForm, setEditForm] = useState({ name: "", base_url: "", api_key: "", model_id: "" });
  const [editFieldIdx, setEditFieldIdx] = useState(0);
  const [editId, setEditId] = useState(""); // non-empty = updating existing
  const [statusMsg, setStatusMsg] = useState("");
  const [testResult, setTestResult] = useState<string | null>(null);

  useEffect(() => { fetchStatus(); }, [fetchStatus]);

  const clampSel = useCallback((idx: number) => {
    return Math.max(0, Math.min(idx, Math.max(0, providers.length - 1)));
  }, [providers.length]);

  const openEdit = useCallback((p?: LLMProvider) => {
    if (p) {
      setEditForm({ name: p.name, base_url: p.base_url, api_key: p.api_key, model_id: p.model_id });
      setEditId(p.id);
    } else {
      setEditForm({ name: "", base_url: "", api_key: "", model_id: "" });
      setEditId("");
    }
    setEditFieldIdx(0);
    setMode("edit");
  }, []);

  const saveEdit = useCallback(async () => {
    const p: LLMProvider = {
      id: editId, name: editForm.name, base_url: editForm.base_url,
      api_key: editForm.api_key, model_id: editForm.model_id, created_at: "",
    };
    if (!p.name || !p.base_url || !p.model_id) {
      setStatusMsg("名称、Base URL、Model ID 为必填项");
      return;
    }
    setStatusMsg("保存中...");
    const result = await saveProvider(p);
    if (result) {
      setStatusMsg(editId ? "已更新" : "已创建");
      setMode("list");
    } else {
      setStatusMsg("保存失败");
    }
  }, [editId, editForm, saveProvider]);

  const handleSwitch = useCallback(async (providerId: string) => {
    setStatusMsg("切换中...");
    const ok = await switchProvider(providerId);
    setStatusMsg(ok ? "已切换" : "切换失败");
  }, [switchProvider]);

  const handleDelete = useCallback(async (providerId: string) => {
    setStatusMsg("删除中...");
    const ok = await deleteProvider(providerId);
    setStatusMsg(ok ? "已删除" : "删除失败");
    if (ok) setSelIdx(clampSel(Math.min(selIdx, providers.length - 2)));
  }, [deleteProvider, providers.length, selIdx, clampSel]);

  const handleTest = useCallback(async (p: LLMProvider) => {
    setTestResult("测试中...");
    try {
      const result: any = await client.callTool("gitgo_agent_chat", {
        project, message: "ping",
      });
      const resp = result?.response || "";
      if (resp && !resp.startsWith("[Mock")) {
        setTestResult(`连通成功: ${resp.slice(0, 80)}...`);
      } else if (resp.includes("Mock")) {
        setTestResult("LLM 返回 Mock（配置可能未生效）");
      } else {
        setTestResult("响应异常");
      }
    } catch (e: any) {
      setTestResult(`连接失败: ${e.message}`);
    }
  }, [client, project]);

  // ── Keyboard ────────────────────────────────────────────

  useInput((input: string, key: any) => {
    if (mode === "edit") {
      if (key.escape) { setMode("list"); setStatusMsg(""); return; }
      if (key.tab) { setEditFieldIdx((f: number) => (f + 1) % 4); return; }
      if (key.shiftTab) { setEditFieldIdx((f: number) => (f + 3) % 4); return; }
      if (key.return) { saveEdit(); return; }

      const field = ["name", "base_url", "api_key", "model_id"][editFieldIdx] as keyof typeof editForm;
      if (key.backspace) {
        setEditForm((f: typeof editForm) => ({ ...f, [field]: f[field].slice(0, -1) }));
        return;
      }
      if (input && input.length >= 1 && !key.ctrl && !key.meta) {
        setEditForm((f: typeof editForm) => ({ ...f, [field]: f[field] + input }));
        return;
      }
      return;
    }

    // List mode
    if (key.escape) { onBack(); return; }
    if (key.upArrow) { setSelIdx((p: number) => clampSel(p - 1)); return; }
    if (key.downArrow) { setSelIdx((p: number) => clampSel(p + 1)); return; }

    if (providers.length === 0) {
      if (input === "n" || input === "N") { openEdit(); return; }
      return;
    }

    if (key.return) {
      const p = providers[selIdx];
      if (p) handleSwitch(p.id);
      return;
    }
    if (input === "n" || input === "N") { openEdit(); return; }
    if (input === "e" || input === "E") {
      const p = providers[selIdx];
      if (p) openEdit(p);
      return;
    }
    if (input === "d" || input === "D") {
      const p = providers[selIdx];
      if (p) handleDelete(p.id);
      return;
    }
    if (input === "t" || input === "T") {
      const p = providers[selIdx];
      if (p) handleTest(p);
      return;
    }
  });

  // ── Render helpers ───────────────────────────────────────

  const maskKey = (key: string) => {
    if (!key) return "(未设置)";
    if (key.length <= 4) return "***";
    return key.slice(0, 4) + "***" + key.slice(-4);
  };

  // ── Render: Edit mode ────────────────────────────────────

  if (mode === "edit") {
    return (
      <Box flexDirection="column" width={cols} paddingTop={1}>
        <Box paddingLeft={1} marginBottom={1}>
          <Text bold>{editId ? "编辑 Provider" : "新建 Provider"}</Text>
          <Text dimColor>    [Esc] 取消</Text>
        </Box>

        {FIELD_LABELS.map((label, i: number) => {
          const field = ["name", "base_url", "api_key", "model_id"][i] as keyof typeof editForm;
          return (
            <Box key={label} flexDirection="row" paddingLeft={1} marginBottom={1}>
              <Text dimColor>{label.padEnd(10)}: </Text>
              <Text color={i === editFieldIdx ? "cyan" : undefined}>
                [{editForm[field]}]
              </Text>
              {i === editFieldIdx ? <Text color="cyan" dimColor>█</Text> : null}
            </Box>
          );
        })}

        <Box paddingLeft={1} marginBottom={1}>
          <Text dimColor>
            [Enter] 保存  [Tab] 下一字段  [Shift+Tab] 上一字段
          </Text>
        </Box>

        {statusMsg ? (
          <Box paddingLeft={1}><Text color="yellow">{statusMsg}</Text></Box>
        ) : null}
      </Box>
    );
  }

  // ── Render: List mode ────────────────────────────────────

  return (
    <Box flexDirection="column" width={cols} paddingTop={1}>
      <Box paddingLeft={1} marginBottom={1}>
        <Text bold>LLM Provider 配置</Text>
        {providers.length > 0 ? (
          <Text dimColor>
            {"  "}
            {providers.find((p: LLMProvider) => p.id === activeProvider)?.name || "未激活"}
            {"  "}共 {providers.length} 个 Provider
          </Text>
        ) : (
          <Text dimColor>  尚未配置 Provider</Text>
        )}
      </Box>

      {loading ? (
        <Box paddingLeft={1}><Text dimColor>加载中...</Text></Box>
      ) : providers.length === 0 ? (
        <Box flexDirection="column" paddingLeft={1} marginBottom={1}>
          <Text dimColor>暂无 LLM Provider。</Text>
          <Text dimColor>按 [N] 新建一个，例如：</Text>
          <Box paddingLeft={2} marginTop={1}>
            <Text dimColor>名称: Groq Llama 3.3</Text>
            <Text dimColor>URL:  https://api.groq.com/openai/v1</Text>
            <Text dimColor>Key:  gsk_your_key_here</Text>
            <Text dimColor>Model: llama-3.3-70b-versatile</Text>
          </Box>
        </Box>
      ) : (
        <Box flexDirection="column">
          {providers.map((p: LLMProvider, i: number) => {
            const isActive = p.id === activeProvider;
            const selected = i === selIdx;
            return (
              <Box key={p.id} flexDirection="column" paddingLeft={1} marginBottom={1}>
                <Box flexDirection="row">
                  <Text color={isActive ? "green" : undefined} bold={selected}>
                    {isActive ? "● " : "○ "}
                    {p.name}
                  </Text>
                  {isActive ? <Text color="green" dimColor>  [当前激活]</Text> : null}
                  {selected && !isActive ? <Text color="cyan" dimColor>  ←</Text> : null}
                </Box>
                <Box paddingLeft={2}>
                  <Text dimColor>base: {p.base_url}</Text>
                </Box>
                <Box paddingLeft={2}>
                  <Text dimColor>model: {p.model_id}</Text>
                </Box>
                <Box paddingLeft={2}>
                  <Text dimColor>key:  {maskKey(p.api_key)}</Text>
                </Box>
              </Box>
            );
          })}
        </Box>
      )}

      <Box flexDirection="column" paddingLeft={1} marginTop={1}>
        <Text dimColor>──────────────────────────────────────────</Text>
        <Box flexDirection="row">
          <Text dimColor>
            [N] 新建  [E] 编辑  [D] 删除  [T] 测试连接
          </Text>
        </Box>
        <Box flexDirection="row">
          <Text dimColor>
            [Enter] 切换激活  ↑↓ 选择  [Esc] 返回
          </Text>
        </Box>
      </Box>

      {statusMsg ? (
        <Box paddingLeft={1} marginTop={1}>
          <Text color="yellow">{statusMsg}</Text>
        </Box>
      ) : null}

      {testResult ? (
        <Box paddingLeft={1} marginTop={1}>
          <Text color={testResult.includes("成功") ? "green" : "red"}>
            {testResult}
          </Text>
        </Box>
      ) : null}
    </Box>
  );
});
