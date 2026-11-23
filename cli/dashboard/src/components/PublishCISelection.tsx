import React, {useCallback, useEffect, useRef, useState} from "react";
import {Box} from "@anthropic/ink";
import type {BackendClient} from "../backend/client.js";
import {publishReleaseSources, type PublishCIArtifact} from "../backend/publish.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {useFeedback} from "../hooks/useFeedback.js";
import {useCopy} from "../ui/PageFrame.js";
import {SearchableChoiceScene} from "./SearchableChoiceScene.js";
import {FeedbackMessage} from "./FeedbackMessage.js";
import type {FooterConfig} from "./CommandBar.js";

const safe = (value: string) => value.replace(/[\x00-\x1f\x7f-\x9f]/g, " ");
const selection = (row: PublishCIArtifact) => ({run_id:row.run_id, attempt:row.attempt, artifact_id:row.artifact_id});

/** Existing version-detail field selection; opening it only reads CI facts. */
export function PublishCISelection({client, project, itemId, planBinding, sourceOid, initial, saving, onFooter, onCancel, onSubmit}: {
  client: BackendClient; project: string; itemId: string; planBinding: string; sourceOid: string;
  initial: PublishCIArtifact[]; saving: boolean; onFooter: (footer: FooterConfig | null) => void; onCancel: () => void;
  onSubmit: (rows: ReturnType<typeof selection>[]) => void;
}) {
  const t=useCopy(), input=useTextInput("");
  const [rows,setRows]=useState<PublishCIArtifact[]>([]), [chosen,setChosen]=useState(initial);
  const [busy,setBusy]=useState(false), [page,setPage]=useState(0), [more,setMore]=useState(false);
  const live=useRef(true), sequence=useRef(0);
  const feedback=useFeedback({kind:"page",id:`publish/ci/${project}/${itemId}/${planBinding}`}, {tone:"error"});
  const read=useCallback(async (next: number) => {
    const owner=++sequence.current; setBusy(true);
    try {
      const value=await publishReleaseSources(client,project,itemId,planBinding,next);
      if (!live.current || owner!==sequence.current) return;
      if (value.source_oid!==sourceOid) throw new Error("PUBLISH_CI_SOURCE_MISMATCH");
      setRows(previous => next===1 ? value.rows : [...previous, ...value.rows.filter(row=>!previous.some(old=>old.artifact_id===row.artifact_id))]);
      setPage(next); setMore(value.has_more); feedback.clear();
    } catch(error: any) {
      if (live.current && owner===sequence.current) feedback.show(String(error?.code ?? error?.message ?? error));
    } finally {if (live.current && owner===sequence.current) setBusy(false);}
  }, [client,project,itemId,planBinding,sourceOid,feedback.show,feedback.clear]);
  useEffect(()=>{live.current=true; void read(1); return()=>{live.current=false; sequence.current++;};},[read]);
  const submit=() => {if(!busy&&!saving) onSubmit(chosen.map(selection));};
  const select=(id: string) => {
    if(busy||saving)return;
    if(id==="refresh"){void read(1);return;}
    if(id==="more"){void read(page+1);return;}
    if(id==="save"){submit();return;}
    const row=rows.find(value=>String(value.artifact_id)===id)??chosen.find(value=>String(value.artifact_id)===id);
    if(!row)return;
    if(!chosen.some(value=>value.artifact_id===row.artifact_id)&&chosen.length>=16){
      feedback.show(t({en:"Select at most 16 CI artifacts.",zh:"最多选择 16 个 CI 产物。"}));return;
    }
    setChosen(previous=> {
      if(previous.some(value=>value.artifact_id===row.artifact_id)) return previous.filter(value=>value.artifact_id!==row.artifact_id);
      return [...previous,row];
    });
  };
  const displayed=[...rows,...chosen.filter(value=>!rows.some(row=>row.artifact_id===value.artifact_id))];
  return <Box flexDirection="column" flexGrow={1} minHeight={0} paddingX={1}>
    <SearchableChoiceScene title={t({en:"CI artifacts",zh:"CI 产物"})} input={input} busy={busy||saving}
      notice={!busy&&page===0?{tone:"info",text:t({en:"CI sources have not been read. Refresh to try again.",zh:"CI 来源尚未读取，可刷新重试。"})}:undefined}
      description={safe(sourceOid)} selectedIds={chosen.map(row=>String(row.artifact_id))}
      items={[
        ...displayed.map(row=>({id:String(row.artifact_id),title:safe(row.name),
          hint:safe(`${row.workflow} · run ${row.run_id} / attempt ${row.attempt} · ${row.size} B`)
            +(!busy&&page>0&&!rows.some(value=>value.artifact_id===row.artifact_id)?t({en:" · Saved selection; absent from this observation",zh:" · 已保存的选择，未出现在本次观察中"}):"")})),
        {id:"save",title:t({en:"Save artifact selection",zh:"保存产物选择"}),alwaysVisible:true},
        {id:"refresh",title:t({en:"Refresh CI sources",zh:"刷新 CI 来源"}),alwaysVisible:true},
        ...(more?[{id:"more",title:t({en:"Read more CI sources",zh:"读取后续 CI 来源"}),alwaysVisible:true}]:[]),
      ]} onSelect={select} onSubmit={submit} onCancel={onCancel} onFooter={onFooter} helpTopic="publish.release"/>
    <FeedbackMessage feedback={feedback.message}/>
  </Box>;
}
