import React from 'react';
import {test,expect} from 'bun:test';
import {PassThrough} from 'node:stream';
import {Box,renderSync} from '@anthropic/ink';
import instances from '../../vendor/ink/src/core/instances.js';
import {cellAt} from '../../vendor/ink/src/core/screen.js';
import {App} from './App.js';
import {InputProvider} from '../input/runtime.js';
import {NativeHostClient,type NativeTaskRequest} from '../backend/client.js';
import {DecisionPanel} from './DecisionPanel.js';
import {answeredDecisionView,DecisionSummary} from './DecisionSummary.js';
import {projectRound} from '../hooks/decisionRound.js';
import type {PendingDecision} from '../types.js';

const decision:PendingDecision={decision_id:'group',task_id:'task',process_id:'p',kind:'clarification',
 question:'Audience?',why_user_must_decide:'Business choice',options:[],allow_free_form:true,answer_mode:'incremental',
 questions:[{node_id:'audience',state_topic:'audience',kind:'clarification',question:'Audience?',why_user_must_decide:'Access rules',
  recommendation:'Prefer staff only.',options:[{label:'Staff only',recommended:true} as any],allow_free_form:true},
 {node_id:'approval',state_topic:'approval',kind:'clarification',question:'Approval?',why_user_must_decide:'Booking workflow',
  recommendation:'Prefer no approval for ordinary rooms.',options:[{label:'No approval',recommended:true} as any],allow_free_form:true}]};

function terminal(columns=90,rows=30){
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns,rows});stdout.on('data',()=>{});
 const stderr:any=new PassThrough();stderr.on('data',()=>{});
 return {stdin,stdout,stderr};
}
const pause=()=>new Promise(r=>setTimeout(r,100));
function frame(ink:any){const s=ink.frontFrame.screen;return Array.from({length:s.height},(_,y)=>Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char||' ').join('')).join('\n');}

test('a current group shows independent questions and advice; an answered subset never appears to answer the whole group',async()=>{
 const io=terminal();const answer=JSON.stringify({answers:{approval:'No approval'}});
 const root=renderSync(<InputProvider><Box width={90}><DecisionPanel decision={projectRound(decision,{},'approval')!}
  selectedIndex={0} composing={false} submitting={false}/></Box></InputProvider>,{...io,patchConsole:false,exitOnCtrlC:false});
 try{await pause();const ink:any=instances.get(io.stdout);const shown=frame(ink);
  expect(shown).toContain('Audience?');expect(shown).toContain('Approval?');
  expect(shown).toContain('Prefer staff only.');expect(shown).toContain('Prefer no approval');
  const view=answeredDecisionView(decision,answer);
  expect(view.questions).toHaveLength(1);expect(view.question).toBe('Approval?');
  root.rerender(<Box width={90}><DecisionSummary decision={decision} answer={answer}/></Box>);await pause();
  expect(frame(ink)).toContain('Approval?');expect(frame(ink)).not.toContain('Audience?');
 }finally{root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();}
});

test('complete App selects an independent question and submits only its real reply before the next frontier',async()=>{
 const io=terminal();let pending:PendingDecision|null=decision;const submitted:any[]=[];
 let messages:any[]=[{role:'assistant',kind:'decision',message_id:'decision:group',content:'Audience?',decision,
  status:'awaiting_user',timestamp:'now'}];
 class Client extends NativeHostClient{
  get ready(){return true;}
  async close(){}
  subscribeEvents(){return()=>{};}
  async callTool(op:string){
   if(op==='config.get')return {language:'en',verbose:false};
   if(op==='project.overview')return {projects:[{name:'sample',workspace:'C:/sample',project_id:'sample',runtime_summary:{state_available:true}}]};
   if(op==='project.enter')return {ok:true};
   if(op==='provider.status')return {providers:[{id:'test',name:'Test',model_id:'test'}],active_provider:'test'};
   if(op==='runtime.status')return {daemon_online:true,processes:{p:{process_id:'p',actor_kind:'supervisor',status:pending?'awaiting_user':'completed',pending_decision:pending}},
    main_conversation:messages,agent_conversations:{},pending_questions:pending?[pending]:[],storage:{level:'ok'}};
   return {};
  }
  async sendTaskStreaming(args:NativeTaskRequest,callbacks:any){
   submitted.push(args);callbacks.onAcknowledged({stage:'accepted',request_id:'transport'});await pause();
   pending={...decision,decision_id:'recomputed',question:'Audience?',questions:[decision.questions![0]!]};
   messages=[{...messages[0],decision:answeredDecisionView(decision,args.message),decision_answer:args.message,status:'answered'},
    {role:'assistant',message_id:'decision:recomputed',content:'Audience?',decision:pending,status:'awaiting_user',timestamp:'now'}];
   callbacks.onAcknowledged({stage:'admitted',process_id:'p',task_id:'task'});
   callbacks.onComplete({outcome:{task_id:'task',process_id:'p',status:'awaiting_user',response:'Audience?',metadata:{pending_decision:pending}}});
  }
 }
 const root=renderSync(<InputProvider><App client={new Client('unused','unused')}/></InputProvider>,{...io,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(io.stdout);ink.setAltScreenActive(true);ink.onRender();
 try{await pause();io.stdin.write('\r');await pause();await pause();expect(frame(ink)).toContain('Prefer staff only.');
  io.stdin.write('\x1b[C');await pause();io.stdin.write('\r');await pause();await pause();await pause();
  expect(submitted).toHaveLength(1);
  expect(submitted[0].decision.decision_id).toBe('group');
  expect(JSON.parse(submitted[0].message)).toEqual({answers:{approval:'Choose option 1: No approval'}});
  expect(frame(ink)).toContain('Audience?');expect(frame(ink)).toContain('Prefer staff only.');
 }finally{root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();}
},10000);
