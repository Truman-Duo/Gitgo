import {SelectionText} from "./Selection.js";
import React, {memo, useState, useEffect, useRef} from "react";
import {Box, Text} from "@anthropic/ink";
import {useManagedInput as useInput} from "../input/runtime.js";
import type {BackendClient} from "../backend/client.js";
import {usePanelSize} from "../theme/index.js";
import {createProject} from "../backend/tools.js";
import {chordLabel} from "../input/bindings.js";
import {resolveCreateProjectKey} from "../input/overlays/createProject.js";
import {applyTextOp, type UseTextInputReturn} from "../hooks/useTextInput.js";
import {useStagedForm} from "../hooks/useStagedForm.js";
import {FormFieldRow, fieldLabelWidth} from "./FormFieldRow.js";
import {cycleChoiceIndex} from "../input/choice.js";
import type {FooterConfig} from "./CommandBar.js";
import {useCopy} from "../ui/PageFrame.js";
import {useFeedback} from "../hooks/useFeedback.js";
import {FeedbackMessage} from "./FeedbackMessage.js";

type Props = {client: BackendClient; defaultWorkspace?: string; onDismiss: () => void;
  onCreated?: (name: string) => void; cmdInput: UseTextInputReturn; onFooter: (cfg: FooterConfig | null) => void};
const FIELDS = ["name", "mode", "workspace", "release"] as const;
const MODE_OPTIONS = ["attach_existing", "create_new"];

export const CreateProjectPanel = memo(function CreateProjectPanel({client, defaultWorkspace, onDismiss, onCreated, cmdInput, onFooter}: Props) {
  const t = useCopy();
  const {w} = usePanelSize({minWidth: 20, widthOffset: 4});
  const form = useStagedForm({name: "", mode: "attach_existing", workspace: defaultWorkspace || "", release: ""}, FIELDS, ["mode"], cmdInput);
  const feedback = useFeedback({kind: "page", id: "create-project"}, {tone: "error"});
  const status = feedback.text, setStatus = feedback.show;
  const [saving, setSaving] = useState(false);
  const submitting = useRef(false);
  const hint = form.editing
    ? `${form.isChoice ? `${chordLabel("leftRight")} ${t({en: "choose", zh: "选择"})} · ` : ""}${chordLabel("enter")} ${t({en: "confirm field", zh: "暂存当前字段"})} · ${chordLabel("escape")} ${t({en: "undo field", zh: "撤销当前字段"})} · ${chordLabel("formSave")} ${t({en: "save form", zh: "保存表单"})}`
    : `${chordLabel("upDown")} / ${chordLabel("tab")} ${t({en: "select", zh: "选择"})} · ${chordLabel("enter")} ${t({en: "edit", zh: "修改"})} · ${chordLabel("formSave")} ${t({en: "save form", zh: "保存表单"})}`;
  useEffect(() => {
    onFooter(form.editing && !form.isChoice ? {kind: "normal", cmdInput, statusText: status || hint,
      suggestions: [], suggestionIdx: 0, cmdResult: ""} : {hidden: true});
    return () => onFooter(null);
  }, [form.editing, form.isChoice, cmdInput.value, cmdInput.cursor, status, hint, onFooter]);
  const submit = async () => {
    if (submitting.current) return;
    const final = form.current;
    const name = final.name.trim(), workspace = final.workspace.trim();
    if (!name || !workspace) {setStatus(t({en: "Name and workspace are required", zh: "请填写项目名称和工作目录"})); return;}
    submitting.current = true; setSaving(true);
    setStatus(final.mode === "attach_existing" ? t({en: "Validating existing folder…", zh: "正在检查已有目录…"}) : t({en: "Creating project…", zh: "正在创建项目…"}),
      {tone: "info", retention: {kind: "until-resolved", reason: "operation-pending"}});
    try {
      const result: any = await createProject(client, {name, workspace_path: workspace,
        workspace_mode: final.mode as "attach_existing" | "create_new", release_url: final.release.trim()});
      if (result?.error || result?.ok === false) throw new Error(String(result.message || result.error || "Project creation failed"));
      onCreated ? onCreated(name) : onDismiss();
    } catch (error: any) {setStatus(String(error?.message || error));}
    finally {submitting.current = false; setSaving(false);}
  };
  useInput((input, key) => {
    if (saving) return;
    for (const action of resolveCreateProjectKey(form.field, input, key, form.editing)) {
      switch (action.type) {
        case "dismiss": onDismiss(); break;
        case "nextField": form.move(1); break;
        case "prevField": form.move(-1); break;
        case "beginEdit": form.begin(); break;
        case "confirmEdit": form.confirm(); break;
        case "cancelEdit": form.cancel(); break;
        case "submit": void submit(); break;
        case "modePrev": case "modeNext": form.setChoice(MODE_OPTIONS[cycleChoiceIndex(MODE_OPTIONS.indexOf(form.current.mode), action.type === "modePrev" ? -1 : 1, MODE_OPTIONS.length)]!); break;
        case "text": applyTextOp(action.op, cmdInput); break;
      }
    }
  }, {bindingContext: "form"});
  const labels = {name: t({en: "Name *", zh: "项目名称 *"}), mode: t({en: "Workspace Mode *", zh: "工作目录模式 *"}),
    workspace: t({en: "Workspace Path *", zh: "工作目录路径 *"}), release: t({en: "Release URL", zh: "发布仓库 URL"})};
  const labelWidth = fieldLabelWidth(Object.values(labels), Math.max(1, w - 10));
  return <Box flexDirection="column" padding={1} flexGrow={1}>
    <Text bold>{t({en: "Create Project", zh: "创建项目"})}</Text>
    <Box flexDirection="column" marginY={1}>{FIELDS.map(field => {
      return <FormFieldRow key={field} label={labels[field]} labelWidth={labelWidth} selected={form.field === field} editing={form.editing && form.field === field}>
        <SelectionText  wrap="truncate-end">{field === "mode" ? form.current.mode === "attach_existing" ? t({en: "Attach Existing", zh: "接入已有目录"}) : t({en: "Create New", zh: "创建新目录"}) : form.current[field] || "—"}</SelectionText>
      </FormFieldRow>;
    })}</Box>
    <Text dimColor>{t({en: "Attach Existing requires an existing folder; Create New requires a missing or empty folder. Uses the configured active provider.", zh: "接入已有目录要求目录存在；创建新目录要求路径不存在或目录为空。模型沿用已配置的当前服务商。"})}</Text>
    {feedback.message ? <Box marginTop={1}><FeedbackMessage feedback={feedback.message}/></Box> : null}
    <Box marginTop={1}><Text dimColor>{hint} · {chordLabel("escape")} {t({en: "cancel", zh: "取消"})}</Text></Box>
  </Box>;
});
