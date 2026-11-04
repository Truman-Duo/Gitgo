import {useLocalHelpCommand} from "../hooks/useLocalHelpCommand.js";
import {SelectionText} from "./Selection.js";
import React, { memo, useEffect, useState, useRef } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import { resolveExportKey } from "../input/overlays/export.js";
import { colors } from "../theme/index.js";
import { exportData } from "../backend/tools.js";
import { chordLabel } from "../input/bindings.js";
import { applyTextOp, type UseTextInputReturn } from "../hooks/useTextInput.js";
import { useStagedForm } from "../hooks/useStagedForm.js";
import { FormFieldRow, fieldLabelWidth } from "./FormFieldRow.js";
import {cycleChoiceIndex} from "../input/choice.js";
import type { FooterConfig } from "./CommandBar.js";
import { useCopy } from "../ui/PageFrame.js";
import {useFeedback} from "../hooks/useFeedback.js";
type Props = {
    client: BackendClient;
    project: string;
    cols: number;
    cmdInput: UseTextInputReturn;
    onFooter: (config: FooterConfig | null) => void;
    onDismiss: () => void;
};
const FIELDS = ["scope", "format", "destination"] as const;
const SCOPES = ["Minimal", "Full"];
const FORMATS = ["json", "yaml", "markdown"];
export const ExportPanel = memo(function ExportPanel({ client, project, cols, cmdInput, onFooter, onDismiss }: Props) {
    const t = useCopy();
    const defaultPath = (values: {
        scope: string;
        format: string;
    }) => `.gitgo/exports/${project}-${values.scope.toLowerCase()}.${values.format === "markdown" ? "md" : values.format}`;
    const form = useStagedForm({ scope: "Minimal", format: "json", destination: defaultPath({ scope: "Minimal", format: "json" }) }, FIELDS, ["scope", "format"], cmdInput);
    const current = { ...form.current };
    if (!(form.editing && form.field === "destination") && form.values.destination === defaultPath(form.values))
        current.destination = defaultPath(current);
    const [status, setExportState] = useState<"idle" | "exporting" | "done" | "error">("idle");
    const feedback = useFeedback({kind: "page", id: `export:${project}`});
    const result = feedback.text, setResult = feedback.show;
    const submitting = useRef(false);
    const menuHelp=useLocalHelpCommand("export",onFooter,!form.editing&&status!=="exporting");
  useEffect(() => {
    if(menuHelp.active)return;
        onFooter(form.editing && !form.isChoice && status !== "done" ? { kind: "normal", cmdInput, suggestions: [], suggestionIdx: 0, cmdResult: "", statusText: "" } : { hidden: true });
        return () => onFooter(null);
    }, [form.editing, form.isChoice, status, cmdInput.value, cmdInput.cursor, onFooter,menuHelp.active]);
    const submit = async () => {
        if (submitting.current)
            return;
        const outputPath = current.destination.trim();
        if (!outputPath) {
            setResult(t({ en: "Destination is required", zh: "请填写导出路径" }));
            setExportState("error");
            return;
        }
        submitting.current = true;
        setExportState("exporting");
        try {
            const value: any = await exportData(client, project, { minimal: current.scope === "Minimal", output_path: outputPath, output_format: current.format as "json" | "yaml" | "markdown" });
            if (value?.error || value?.ok === false)
                throw new Error(String(value.message || value.error || "Export failed"));
            setResult(`${value.path}   ${value.size} bytes   sha256 ${String(value.sha256 || "").slice(0, 12)}`);
            setExportState("done");
            form.cancel();
        }
        catch (error: any) {
            setResult(String(error?.message || error));
            setExportState("error");
        }
        finally {
            submitting.current = false;
        }
    };

  useInput((input, key) => {
    if(menuHelp.handle(input,key))return;
        for (const action of resolveExportKey(status, form.field, input, key, form.editing)) {
            if (action.type === "dismiss")
                onDismiss();
            else if (action.type === "moveField")
                form.move(action.delta);
            else if (action.type === "beginEdit")
                form.begin();
            else if (action.type === "confirm")
                form.confirm(current);
            else if (action.type === "cancelEdit")
                form.cancel();
            else if (action.type === "submit")
                void submit();
            else if (action.type === "text")
                applyTextOp(action.op, cmdInput);
            else if (action.type === "cycle") {
                const choices = form.field === "scope" ? SCOPES : FORMATS;
                form.setChoice(choices[cycleChoiceIndex(choices.indexOf(current[form.field]), action.delta, choices.length)]!);
            }
        }
    }, { bindingContext: "form",clipboard:form.editing&&!form.isChoice&&status!=="exporting",inputTarget:form.editing?form.field:"" });
    const labels = { scope: t({ en: "Scope", zh: "范围" }), format: t({ en: "Format", zh: "格式" }), destination: t({ en: "Destination", zh: "导出路径" }) };
    return <Box flexDirection="column" padding={1} width={cols}>
    <Box marginBottom={1}><Text bold>{t({ en: "Export", zh: "导出" })} — {project}</Text></Box>
    {FIELDS.map(field => {
            const active = form.field === field;
            return <FormFieldRow key={field} label={labels[field]} labelWidth={fieldLabelWidth(Object.values(labels), Math.max(1, cols - 12))} selected={active} editing={active && form.editing}>
      <SelectionText  wrap="truncate-end">{current[field]}</SelectionText>
    </FormFieldRow>;
        })}
    <Box marginTop={1}></Box>
    {status !== "idle" ? <Box marginTop={1}><Text color={status === "error" ? colors.danger : status === "done" ? colors.success : undefined}>{status === "exporting" ? t({ en: "Exporting…", zh: "正在导出…" }) : result}</Text></Box> : null}
  </Box>;
});
