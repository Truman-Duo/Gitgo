import {PageFrame,useCopy} from "../ui/PageFrame.js";
import {pages,type Copy} from "../ui/pageContract.js";
import React, { type ReactNode } from "react";
import { Box } from "@anthropic/ink";
import { usePanelSize } from "../theme/index.js";
import {PageHeader} from "../ui/PageHeader.js";

export const RUNTIME_TABS = [
  { id: "lesson" },
  { id: "contract" },
  { id: "governance" },
  { id: "memory" },
  { id: "context" },
  { id: "tools" },
] as const;

export function runtimeTabs(t:(value:Copy)=>string) {
 const labels=[t({en:"lesson",zh:"经验"}),t({en:"contract",zh:"契约"}),t({en:"governance",zh:"治理"}),
  t({en:"memory",zh:"记忆"}),t({en:"context",zh:"上下文"}),t({en:"actions",zh:"自定义动作"})];
 return RUNTIME_TABS.map((item,index)=>({...item,label:labels[index]!}));
}
export function RuntimeChildShell({ tab, children }: { tab: string; children: ReactNode }) {
  const t=useCopy();
  const { w } = usePanelSize({ minWidth: 40, widthOffset: 4 });
  const selected = Math.max(0, RUNTIME_TABS.findIndex(item => item.id === tab));
  return <PageFrame page={pages.runtime}>
    <PageHeader items={runtimeTabs(t)} selected={selected} width={w} contentFocused/>
    <Box flexDirection="column" flexGrow={1} minHeight={0}>{children}</Box>
  </PageFrame>;
}
