import {expect, test} from "bun:test";
import {projectRuntimeDecision} from "./decisionCopy.js";
import type {PendingDecision} from "../types.js";
import type {Copy} from "./pageContract.js";

function card(template:string,actions:string[]):PendingDecision {
  return {decision_id:"same-card",task_id:"same-task",process_id:"same-process",display_template:template,
    question:"Original question",why_user_must_decide:"Original evidence",allow_free_form:false,
    permission_request:{purpose:"read notes",tool_name:"read_file",resource:"C:/fixture/notes.txt",effect:"read",
      approval_scope:"exact_read_object_and_arguments"},
    options:actions.map(action=>({action,label:action,principle:"Original principle",immediate_effect:"Original effect",
      downstream_effect:"Original next",risks:"Original risk",reversibility:"Original recovery",recommended:false}))};
}

test.each([
  ["permission",["allow_once","allow_task","deny"]],
  ["context_force_compaction",["force_compact","stop"]],
  ["context_compaction_maintenance",["force_compact","stop"]],
  ["completion_exception",["continue","accept_partial","stop"]],
  ["completion_exception",["continue","amend_scope","stop"]],
  ["engineering_amendment",["accept_engineering_amendment","keep_engineering_workflow"]],
] as [string,string[]][])("%s translates all fixed fields without changing authorization or evidence",(template,actions)=>{
  const source=card(template,actions);
  source.display_context={reason:"用户原话",changes:{nodes:[{id:"actual-node",before:"original",after:"proposed"}]}};
  const before=JSON.stringify(source);
  for(const language of ["zh","en","zh"] as const){
    const shown=projectRuntimeDecision(source,(copy:Copy)=>copy[language]);
    expect(shown.decision_id).toBe(source.decision_id);expect(shown.task_id).toBe(source.task_id);
    expect(shown.process_id).toBe(source.process_id);expect(shown.permission_request).toBe(source.permission_request);
    expect(shown.options.map(option=>option.action)).toEqual(actions);
    expect(shown.question).not.toContain("Original question");
    for(const option of shown.options)for(const field of ["label","principle","immediate_effect","downstream_effect","risks","reversibility"] as const){
      expect(option[field]).toBeTruthy();expect(option[field]).not.toMatch(/^Original /u);
      if(language==="zh")expect(option[field]).toMatch(/[\u4e00-\u9fff]/u);
    }
    expect(JSON.stringify(source)).toBe(before);
  }
});

test("ordinary questions, historical text and unknown templates remain verbatim",()=>{
  for(const template of ["","future-template"]){
    const source=card(template,["unchanged"]);
    expect(projectRuntimeDecision(source,copy=>copy.zh)).toBe(source);
  }
});

test("execution rules show the restored tool range, sample match and unavailable tool",()=>{
  const source = card("task_execution_policy",["apply","cancel"]);
  source.display_context = {
    previous_parameters:{include_children:true,allow_only:["read_file"],rules:[]},
    parameters:{include_children:true,allow_only:null,rules:[{tool:"shell_script",action:"deny",arguments:{cwd:"fixture"}}]},
    not_mounted_tools:["shell_script"],
    match_example:{tool:"shell_script",arguments:{cwd:"fixture"}},
  };
  const shown = projectRuntimeDecision(source,copy=>copy.zh);
  expect(shown.question).toContain("当前\n当前任务及子任务\nread_file");
  expect(shown.question).toContain("拟生效\n当前任务及子任务\n默认工具范围");
  expect(shown.question).toContain('匹配示例 · shell_script {"cwd":"fixture"}');
  expect(shown.question).toContain("当前进程未挂载 · shell_script");
  expect(shown.options.map(option=>option.action)).toEqual(["apply","cancel"]);
});

test("maintenance cancellation and a live-task stop retain distinct consequences",()=>{
  const stop=(template:string)=>projectRuntimeDecision(card(template,["stop"]),copy=>copy.zh).options[0]!;
  expect(stop("context_compaction_maintenance").immediate_effect).toContain("保持会话不变");
  expect(stop("context_force_compaction").immediate_effect).toContain("停止当前任务");
});

test("the legacy typed permission contract localizes history without changing its original evidence",()=>{
  const source = {...card("", ["allow_once", "allow_task", "deny"]), kind:"permission"};
  const before = JSON.stringify(source);
  const shown = projectRuntimeDecision(source, copy=>copy.zh);
  expect(shown.question).toContain("是否允许");
  expect(shown.why_user_must_decide).toContain("所示工具、资源和访问范围");
  expect(shown.options.map(option=>option.action)).toEqual(source.options.map(option=>option.action));
  expect(shown.options[0]!.principle).toContain("本次");
  expect(shown.permission_request).toBe(source.permission_request);
  expect(JSON.stringify(source)).toBe(before);
  const future = {...source, options:[...source.options, {...source.options[0]!, action:"future-permission"}]};
  expect(projectRuntimeDecision(future, copy=>copy.zh)).toBe(future);
});
