/* Handoff UI reference; final design belongs to the implementer. Server facts drive workflow; navigation never mutates it. */
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const short=v=>v?String(v).slice(0,8):'尚无';
const phaseNames={prepare:'准备材料',check:'运行检查',route:'选择接纳方式',handoff:'填写接手要求',compose:'整理正式草稿',apply:'执行接纳',push:'推送本地远端',done:'处理结果'};
const statusNames={ready:'待继续',running:'进行中',awaiting:'待处理',paused:'已暂停',blocked:'受阻',done:'已完成'};
let data=null, busy=false, rows=[], commandRows=[], commandOpen=false, commandReturn=null;
let ui={scene:'inbox',id:null,row:0,focus:'header'}, stack=[], drafts=new Map(), form=null;
let notice='', noticeError=false;
const nodeChoices=new Map();
const stepStates={passed:'已完成',failed:'未通过',pending:'未开始',ready:'待执行',waiting:'待你处理',running:'执行中',paused:'已暂停',skipped:'不执行'};
const stepMarks={passed:'✓',failed:'!',pending:'○',ready:'◆',waiting:'◆',running:'◐',paused:'Ⅱ',skipped:'−'};

function current(){const context=commandOpen&&commandReturn?commandReturn:ui;return context.scene==='inbox'?data?.items[context.row]:data?.items.find(i=>i.id===context.id);}
function notify(message,error=false){notice=message;noticeError=error;$('notice').textContent=message;$('notice').classList.toggle('error',error);}
async function refresh(){
  const r=await fetch('/api/state');if(!r.ok)throw Error('无法读取本地演示状态');
  const next=await r.json();const changed=!data||data.revision!==next.revision;data=next;
  if(changed)render();
}
async function act(action,args={},id=current()?.id??null){
  if(busy)return false;busy=true;notify(action==='reset'?'正在新建示例…':'');
  const item=data.items.find(i=>i.id===id);
  try{
    const response=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json','X-Demo-Client':'terminal-flow'},body:JSON.stringify({request_id:crypto.randomUUID(),action,item:id,version:item?.version,args})});
    const result=await response.json();if(response.ok&&result.ok)nodeChoices.delete(id);await refresh();
    if(!response.ok||!result.ok)throw Error(result.error?.message||'操作失败');
    if(action==='reset')notify('');
    return true;
  }catch(error){notify(error.message,true);return false;}finally{busy=false;render();}
}
function go(scene,id=current()?.id??null,extra={}){
  saveDraft();stack.push({...ui,scroll:$('surface').scrollTop,savedForm:form?{...form}:null});
  ui={scene,id,row:0,focus:'content',...extra};form=null;commandOpen=false;$('command-input').value='';render();$('surface').scrollTop=0;$('terminal').focus();
}
function back(){
  if(commandOpen){closeCommands();return;}
  saveDraft();form=null;notify('');
  if(stack.length){ui=stack.pop();form=ui.savedForm||null;delete ui.savedForm;if(form)$('normal-input').value=drafts.get(form.key)??form.initial;render();$('surface').scrollTop=ui.scroll||0;}
  else{ui.focus='header';render();}
  if(form)$('normal-input').focus();else $('terminal').focus();
}
function saveDraft(){if(form)drafts.set(form.key,$('normal-input').value);}
function openForm(kind,initial,label,extra={}){
  go('form');form={kind,key:(ui.id||'settings')+':'+kind,label,initial,...extra};
  $('normal-input').value=drafts.get(form.key)??initial;ui.focus='normal';render();$('normal-input').focus();
}
async function submitForm(){
  if(!form)return;const f={...form},text=$('normal-input').value;saveDraft();let ok=false;
  if(['handoff','compose','note'].includes(f.kind))ok=await act(f.kind,{text});
  else if(f.kind==='branch')ok=await act('plan',{key:'branch',value:text.trim()});
  else if(f.kind==='prefix')ok=await act('settings',{section:'commit_format',value:{...data.settings.commit_format,prefix:text.trim()}},null);
  else if(f.kind==='excludes')ok=await act('settings',{section:'privacy',value:{...data.settings.privacy,exclude_paths:text.split(/[;\n]/).map(x=>x.trim()).filter(Boolean)}},null);
  else if(f.kind==='remote')ok=await act('settings',{section:'remote_url',value:text.trim()},null);
  if(ok){drafts.delete(f.key);back();if(['handoff','compose'].includes(f.kind)){ui.scene='node';ui.node=current().workflow.current;ui.row=0;ui.focus='content';render();}notify(f.kind==='note'?'已保存到本地讨论记录':'已保存');}
}
function row(label,tail,run,hint='',disabled=false){rows.push({label,tail,run,hint,disabled});}
function rowHTML(){
  ui.row=Math.max(0,Math.min(ui.row,rows.length-1));
  return '<div class="rows">'+rows.map((r,n)=>`<button class="row ${n===ui.row?'selected':''} ${r.disabled?'disabled':''}" data-row="${n}" aria-selected="${n===ui.row}" aria-disabled="${r.disabled}"><span class="marker">${n===ui.row?'❯':' '}</span><span class="name">${esc(r.label)}</span><span class="tail">${esc(r.tail)}</span></button>`).join('')+'</div>'+(rows[ui.row]?.hint?`<div class="hint">${esc(rows[ui.row].hint)}</div>`:'');
}
function facts(entries){return '<div class="facts">'+entries.map(([a,b])=>`<div class="fact"><span class="label">${esc(a)}</span><span class="value">${esc(b)}</span></div>`).join('')+'</div>';}
function events(item,count=4){return item.events.slice(-count).map(e=>`<div class="history-line"><time>${esc(new Date(e.time).toLocaleTimeString('zh-CN',{hour12:false}))}</time><span>${esc(e.text)}</span></div>`).join('');}
function issueContext(item){return `<div class="context">${item.kind==='incoming'?'Trial · 传入贡献 #17':'Workspace · 本地产出'}　${esc(item.label)} / ${short(item.source)}　${esc(item.author)}</div>`;}
function targetLabel(item){return item.plan.target==='workspace'?'Workspace / '+item.plan.branch:'Release / master';}
function statusText(item){if(item.status==='done')return item.decision==='discard'?'暂不采用':item.result?.role==='workspace'?'已接手 · 待完善':'已形成正式版本';if(item.status==='running')return '正在'+phaseNames[item.phase];if(item.check?.state==='failed'&&item.phase==='route')return '检查失败 · 待选择';return statusNames[item.status]+' · '+phaseNames[item.phase];}

function selectedNode(item){
  const steps=item.workflow.steps;
  return steps.find(s=>s.key===nodeChoices.get(item.id))||steps.find(s=>s.current)||steps[0];
}
function miniFlow(item,interactive=false){
  const selected=selectedNode(item);
  return `<div class="mini-scroll"><div class="mini-flow" role="group" aria-label="${esc(item.title)}的小流程">`+item.workflow.steps.map(s=>
    `<${interactive?'button':'span'} class="mini-node ${s.state} ${s.current?'is-current':''} ${interactive&&s.key===selected.key?'node-selected':''}" ${interactive?`data-node="${s.key}" data-item="${item.id}" aria-pressed="${s.key===selected.key}"`:''} aria-label="${esc(s.label+' · '+stepStates[s.state]+(s.current?' · 当前步骤':''))}"><span class="node-mark">${stepMarks[s.state]}</span><span class="node-label">${esc(s.label)}</span></${interactive?'button':'span'}>`
  ).join('')+'</div></div>';
}
function routeSummary(item){
  const source=(item.source_role==='trial'?'Trial':'Workspace')+' / '+item.label;
  if(item.decision==='discard')return source+' · 本次暂不采用，来源保留';
  if(item.result)return source+' ─→ '+(item.result.role==='workspace'?'Workspace':'Release')+' / '+item.result.branch+' · '+short(item.result.oid);
  return source+' ···→ '+targetLabel(item)+(item.decision?' · 已选去向，尚未写入':' · 计划去向');
}
function nodeHint(item,node){
  if(node.state==='skipped')return '本次不执行此节点；Enter 查看处理决定。';
  if(node.state==='pending')return '尚未到达；Enter 查看前置条件与后续计划。';
  if(node.key==='check'&&node.state==='failed')return '检查未通过；Enter 查看实际日志，或接收作者修订后重新检查。';
  if(node.current)return item.error||`${phaseNames[item.phase]} · Enter ${item.status==='running'?'查看执行与暂停操作':item.status==='done'?'查看实际结果':'进入处理'}`;
  return '已有结果；Enter 查看对应材料和记录。';
}
function openNode(key=selectedNode(current()).key,item=current()){
  nodeChoices.set(item.id,key);go('node',item.id,{node:key});
}
function moveNode(delta){
  const item=current();if(!item)return;
  const steps=item.workflow.steps,index=steps.findIndex(s=>s.key===selectedNode(item).key);
  nodeChoices.set(item.id,steps[Math.max(0,Math.min(steps.length-1,index+delta))].key);
  render();document.querySelector('.node-selected')?.scrollIntoView({block:'nearest',inline:'nearest'});
}
function inbox(){
  ui.row=Math.max(0,Math.min(ui.row,data.items.length-1));
  data.items.forEach(item=>row(item.title,statusText(item),()=>openNode(selectedNode(item).key,item)));
  const unfinished=data.items.filter(i=>i.result?.role==='workspace').length;
  return `<div class="inbox-caption">${data.items.filter(i=>i.status!=='done').length} 项待流转${unfinished?' · '+unfinished+' 项接手后待完善':''} <span>↑↓ 事项　←→ 节点　Enter 处理</span></div><div class="work-items">`+data.items.map((item,n)=>{
    const selected=n===ui.row,expanded=selected&&ui.focus!=='header',node=selectedNode(item);
    return `<article class="work-item ${selected?'chosen':''}" data-work="${item.id}"><button class="row ${selected?'selected':''}" data-row="${n}" aria-selected="${selected}"><span class="marker">${selected?'❯':' '}</span><span class="name"><span class="item-id">${item.kind==='incoming'?'#17':'本地'}</span>${esc(item.title)}</span><span class="tail">${esc(statusText(item))}</span></button>`+
      (expanded?`<div class="item-expansion"><div class="route-summary">${esc(routeSummary(item))}</div>${miniFlow(item,true)}<div class="node-context"><span class="${node.state==='failed'?'red':node.current?'blue':'dim'}">${esc(node.label)} · ${stepStates[node.state]}</span><span>${esc(nodeHint(item,node))}</span></div><div class="item-meta">${esc(item.author)} · ${esc(item.label)} / ${short(item.source)}${item.check?.state==='failed'?' · 检查问题仍保留':''}</div></div>`:'')+'</article>';
  }).join('')+'</div>';
}
function nodePage(item){
  const node=item.workflow.steps.find(s=>s.key===ui.node)||item.workflow.steps.find(s=>s.current);
  if(node.current)return itemPage(item);
  let body=issueContext(item)+`<div class="node-page-caption">${esc(node.label)} · ${stepStates[node.state]} <span>当前：${phaseNames[item.phase]}</span></div>`+miniFlow(item);
  if(node.state==='pending'||node.state==='skipped'){
    body+=`<p class="intro">${node.state==='skipped'?'已选择暂不采用，本次不执行此节点。':`尚未到达此步骤。先完成「${phaseNames[item.phase]}」。`}</p>`;
    body+=facts([['当前去向',targetLabel(item)],['执行规则',node.key==='apply'?'接手需要交接要求；正式接纳需要候选、草稿与组合检查。':'由当前计划与实际执行结果推进。']]);
  }else if(node.key==='prepare'){
    body+=facts([['固定来源',short(item.evidence?.source)],['变更文件',item.evidence?.paths.length||0],['准备时间',item.evidence?.created||'']]);
    row('查看固定版本差异','',()=>go('diff'));
  }else if(node.key==='check')return body+checksPage(item);
  else if(node.key==='route'){
    body+=repositoryMap(item)+facts([['处理决定',item.decision==='discard'?'暂不采用':targetLabel(item)],['是否写入',item.result?'已有实际结果':'尚未写入']]);
    if(!item.result&&item.decision!=='discard')row('调整后续去向','',()=>go('plan'));
  }else if(node.key==='write')body+=`<pre class="text">${esc(item.plan.target==='workspace'?item.note:item.message)}</pre>`;
  else if(node.key==='apply')body+=repositoryMap(item)+facts([['实际目标',item.result?.role+' / '+item.result?.branch],['实际提交',short(item.result?.oid)]]);
  else if(node.key==='push')body+=facts([['远端实际提交',short(item.pushed?.remote_tip)]]);
  row('进入当前节点',phaseNames[item.phase],()=>openNode(item.workflow.current,item));
  row('返回事项列表','',home);
  return body+rowHTML();
}
function itemPage(item){
  let body=issueContext(item)+`<div class="status ${item.status==='blocked'?'red':item.status==='done'?'green':''}">${esc(statusText(item))}</div>`;
  if(item.error)body+=`<p class="red text">${esc(item.error)}</p>`;
  if(item.status==='running'){
    body+=facts([['当前动作',phaseNames[item.phase]],['处理对象',item.label+' / '+short(item.source)],['预期去向',targetLabel(item)],['执行方式',item.phase==='prepare'||item.phase==='check'?'完成当前动作后，按准备策略推进':'按已确认内容执行']]);
    row('暂停后续步骤','保留已完成结果',()=>act('pause'));
    return body+events(item)+rowHTML();
  }
  if(item.status==='paused'){
    body+=facts([['停留位置',phaseNames[item.phase]],['后续去向',targetLabel(item)]]);
    row('继续当前步骤','Enter',()=>act('resume'));
    row('调整后续流程','',()=>go('plan'));
    return body+rowHTML()+`<div class="section">最近结果</div>`+events(item);
  }
  if(item.phase==='prepare'){
    body+=facts([['当前来源',item.source_role==='trial'?'Trial / '+item.source_ref:'Workspace / '+item.source_ref],['准备内容','固定版本、文件差异、出站检查、测试'],['准备策略',item.plan.auto_prepare?'连续执行准备与检查':'每步手动继续']]);
    row('准备材料并开始处理',item.plan.auto_prepare?'自动运行后续检查':'先准备材料',()=>act('continue'));
    row('调整流程','去向与自动推进',()=>go('plan'));
  }else if(item.phase==='check'){
    body+=facts([['已准备',item.evidence.paths.length+' 个变更文件'],['固定来源',short(item.source)]]);
    row('运行候选检查','',()=>act('continue'));
    row('查看差异','',()=>go('diff'));
  }else if(item.phase==='route'){
    const failed=item.check?.state==='failed';
    body+=repositoryMap(item,true);
    body+=`<p class="intro">${failed?'空名称边界检查未通过。可以先接手继续加工，原问题保留。':'候选检查已通过。请选择这份贡献的去向。'}</p>`;
    row('接到 Workspace，继续加工','工作分支',async()=>{if(await act('route',{choice:'workspace'}))openForm('handoff',current().note||'先接手这份实现；请 Lin 补齐空名称边界，完成后重新检查。','接手要求');},'创建工作分支，记录后续要求；不会进入正式线。');
    row('接纳到 Release','正式版本',async()=>{if(await act('route',{choice:'release'}))openForm('compose',current().message,'正式提交说明');},failed?'当前检查失败。先查看检查结果或接收作者修订。':'整理正式草稿，再按计划写入正式仓库。',failed);
    row('本次暂不采用','保留来源',()=>act('route',{choice:'discard'}),'记录处理决定，保留来源提交。');
    row('查看检查结果',failed?'1 项失败':'通过',()=>go('checks'));
  }else if(item.phase==='handoff'){
    body+=facts([['接手位置',targetLabel(item)],['尚未完成',item.check?.state==='failed'?'候选检查失败，需要继续修订':'等待补充接手要求']]);
    row('填写接手要求','',()=>openForm('handoff',item.note||'先接手继续完善，完成后重新检查。','接手要求'));
    row('改变接纳方式','',()=>go('plan'));
  }else if(item.phase==='compose'){
    body+=`<div class="intro">${item.status==='blocked'?'当前候选未通过检查，草稿暂不能接纳。':'检查通过。将开发提交整理成一份正式草稿。'}</div><pre class="text">${esc(item.message)}</pre>`;
    row('编辑并确认正式草稿','',()=>openForm('compose',item.message,'正式提交说明'),'',item.status==='blocked');
    row('查看差异','',()=>go('diff'));
    row('查看检查结果','',()=>go('checks'));
  }else if(item.phase==='apply'){
    body+=repositoryMap(item);
    body+=facts([['采用内容',item.label+' / '+short(item.source)],['目标',targetLabel(item)],['接纳方式',item.plan.target==='workspace'?'创建工作分支，继续加工':'整理为一个正式提交'],['结束边界',item.plan.push?'推送到本地演示远端':'留在本地，不推送']]);
    if(item.note)body+=`<div class="intro">${esc(item.note)}</div>`;
    if(item.plan.target==='release')body+=`<pre class="text">${esc(item.message)}</pre>`;
    row('执行当前计划','Enter',()=>act('continue'));
    row('调整后续流程','',()=>go('plan'));
    row('重新准备材料','目标或输入变化时使用',()=>act('reprepare'));
  }else if(item.phase==='push'){
    body+=facts([['正式提交',short(item.result.oid)],['推送目标','本地演示 remote']]);
    row('推送并核对远端','',()=>act('continue'));
  }else{
    body+=repositoryMap(item);
    if(item.result)body+=facts([['实际去向',item.result.role==='workspace'?'Workspace / '+item.result.branch:'Release / master'],['实际提交',short(item.result.oid)],['后续状态',item.result.role==='workspace'?'已接手；原检查问题仍待完善':item.pushed?'已推送本地远端':'正式版本已保留在本地']]);
    if(item.note)body+=`<div class="intro">${esc(item.note)}</div>`;
    row('返回事项列表','',()=>home());
    row('查看本次过程','',()=>go('history'));
    row('查看三仓流转','',()=>go('flow'));
  }
  return body+rowHTML();
}
function resumeInput(item){if(item.phase==='handoff')openForm('handoff',item.note,'接手要求');else if(item.phase==='compose')openForm('compose',item.message,'正式提交说明');else{ui.focus='content';render();}}
function home(){const id=current()?.id;saveDraft();form=null;stack=[];ui={scene:'inbox',id:null,row:Math.max(0,data.items.findIndex(i=>i.id===id)),focus:'content'};commandOpen=false;render();$('terminal').focus();}
function planPage(item){
  const frozen=!!item.result;
  row('准备与检查',item.plan.auto_prepare?'连续执行':'逐步执行',()=>act('plan',{key:'auto_prepare',value:!item.plan.auto_prepare}),'连续执行会在需要选择或填写内容时停下。',frozen);
  row('确认内容以后',item.plan.auto_apply?'自动执行接纳':'等待手动继续',()=>act('plan',{key:'auto_apply',value:!item.plan.auto_apply}),'明确接纳方式、填写内容后，是否自动执行既定操作。',frozen);
  row('预期去向',item.plan.target==='workspace'?'Workspace':'Release',()=>act('plan',{key:'target',value:item.plan.target==='workspace'?'release':'workspace'}),'调整后重新进入接纳方式选择；已执行结果不会被撤销。',frozen||item.kind==='local');
  row('目标分支',item.plan.branch,()=>openForm('branch',item.plan.branch,'工作分支名'),'分支名称在底部蓝色输入栏填写。',frozen||item.plan.target==='release');
  row('正式接纳之后',item.plan.push?'推送本地演示远端':'停在本地',()=>act('plan',{key:'push',value:!item.plan.push}),'只有实际进入 Release 后才适用；演示不会连接外部平台。',frozen);
  return issueContext(item)+`<div class="intro">调整尚未执行的部分</div>`+rowHTML();
}
function checksPage(item){
  if(!item.check)return '<div class="empty">尚未运行检查。返回当前步骤开始处理。</div>';
  const privacy=item.evidence?.privacy;
  let content=facts([['对应候选',short(item.check.oid)],['候选检查',item.check.state==='passed'?'通过':'失败'],['出站扫描',privacy?.blocked?'发现阻止项':'当前规则未发现阻止项']]);
  content+=`<pre class="log">${esc(item.check.log)}</pre>`;
  if(item.check.state==='failed'&&!item.result&&item.kind==='incoming')row('接收作者修订 A4','演示操作',async()=>{if(await act('revision')){go('item');notify('已生成实际修订提交，旧候选及检查记录保留');}},'隔离示例会补齐空名称判断，并产生新的 Git 提交。');
  row('返回当前步骤','',()=>back());return content+rowHTML();
}
function diffPage(item){
  if(!item.evidence)return '<div class="empty">先准备材料，再查看固定版本的差异。</div>';
  return issueContext(item)+item.evidence.diff.split('\n').map(line=>`<span class="diff-line ${line.startsWith('+++')||line.startsWith('---')||line.startsWith('@@')||line.startsWith('diff ')?'diff-head':line.startsWith('+')?'diff-add':line.startsWith('-')?'diff-del':''}">${esc(line)}</span>`).join('');
}
function discussionPage(item){
  let body=issueContext(item)+`<div class="intro">当前事项的本地讨论记录</div>`;
  if(!item.notes.length)body+='<div class="empty">尚无记录。可补充要求或交接说明。</div>';
  body+=item.notes.map(n=>`<div class="green">❯ ${esc(n.author)} <span class="muted">${short(n.source)}</span></div><div class="message user">${esc(n.text)}</div>`).join('');
  row('补充讨论','本地保存',()=>openForm('note','','补充讨论'),'此演示未连接 Agent 或外部评论服务，不生成模型回复。');
  return body+rowHTML();
}
function settingsPage(){
  row('提交格式','编号与模板',()=>go('format',null));
  row('隐私','出站范围与扫描',()=>go('privacy',null));
  row('远端','Release 目标',()=>go('remote',null));
  return rowHTML();
}
function formatPage(){
  row('提交前缀',data.settings.commit_format.prefix,()=>openForm('prefix',data.settings.commit_format.prefix,'提交前缀'));
  row('模板',data.settings.commit_format.template_name,()=>notify('沿用现有 default 模板；新整理的草稿会使用当前前缀。'));
  return '<div class="intro">应用于之后整理的正式草稿</div>'+rowHTML();
}
function privacyPage(){
  row('隐私扫描',data.settings.privacy.enabled?'开启':'关闭可选扫描',()=>act('settings',{section:'privacy',value:{...data.settings.privacy,enabled:!data.settings.privacy.enabled}},null),'高风险凭据检查仍然执行。');
  row('排除路径',data.settings.privacy.exclude_paths.length+' 条',()=>openForm('excludes',data.settings.privacy.exclude_paths.join('\n'),'排除路径 · 每行一条'));
  return '<div class="intro">用于材料出站及正式接纳检查</div>'+rowHTML();
}
function remotePage(){
  row('Release 远端','编辑地址',()=>openForm('remote',data.settings.remote_url,'Release 远端地址'));
  return `<pre class="text">${esc(data.settings.remote_url)}</pre><div class="hint">此演示只允许向自身的本地 bare Git 远端推送。</div>`+rowHTML();
}
function formPage(item){
  if(!form)return '';
  let body=item?issueContext(item):'';
  body+=`<div class="intro">${esc(form.label)}</div>`;
  if(form.kind==='handoff')body+=facts([['接手目标',targetLabel(item)],['当前检查',item.check?.state==='passed'?'通过':'未通过；接手后继续完善'],['保存以后',item.plan.auto_apply?'按当前计划自动接纳':'返回计划，等待手动执行']]);
  if(form.kind==='compose')body+=facts([['采用版本',item.label+' / '+short(item.source)],['写入目标','Release / master'],['保存以后',item.plan.auto_apply?'按当前计划自动执行':'保存草稿，等待手动执行']]);
  if(form.kind==='note')body+='<div class="muted">保存到当前事项的本地记录。</div>';
  return body+'<div class="hint">在底部蓝色输入栏填写。Enter 保存，Shift+Enter 换行；Esc 返回并保留草稿。</div>';
}
function repositoryMap(item,choices=false){
  // Business transfer edges, not Git commit-parent edges. Dashed edges are proposals.
  const sourceY=item.source_role==='trial'?38:105;
  const destination=item.result?.role||item.plan.target;
  let svg='<svg viewBox="0 0 670 216" width="670" height="216" role="img" aria-label="三仓流转：实际位置与计划去向">';
  ['Trial','Workspace','Release'].forEach((label,n)=>{const y=38+n*67;svg+=`<text x="0" y="${y+4}" class="graph-label">${label}</text><path d="M105 ${y}H650" stroke="#303030" fill="none"/>`;});
  const color=item.source_role==='trial'?'#d29922':'#58a6ff';
  svg+=`<path d="M115 ${sourceY}H235" stroke="${color}" class="graph-edge"/><circle cx="235" cy="${sourceY}" r="4" fill="${color}"/><text x="115" y="${sourceY-15}" class="graph-label">${esc(item.label)} / ${short(item.source)}</text>`;
  if(item.decision!=='discard'){
    const targets=choices?['workspace','release']:[destination];
    targets.forEach((role,n)=>{
      const y=role==='workspace'?105:172,actual=item.result?.role===role;
      const selected=choices?ui.row===n:role===destination;
      const blocked=role==='release'&&item.check?.state!=='passed'&&!actual;
      const stroke=actual?'#3fb950':selected?'#58a6ff':'#6e7681',bend=choices&&n===1?360:300;
      svg+=`<path d="M235 ${sourceY}H${bend-8}Q${bend} ${sourceY} ${bend} ${sourceY+8}V${y-8}Q${bend} ${y} ${bend+8} ${y}H535" stroke="${stroke}" ${actual?'':'stroke-dasharray="4 5"'} class="graph-edge"/><circle cx="535" cy="${y}" r="4" fill="${actual?stroke:'#101010'}" stroke="${stroke}"/><text x="400" y="${y+22}" class="graph-title">${actual?short(item.result.oid)+' · 已写入':blocked?'检查未通过 · 不可接纳':role==='workspace'?'接手为工作分支':'整理为正式提交'}</text>`;
    });
  }
  return `<div class="graph-wrap repository-map">${svg}</svg></div><div class="map-caption">${item.result?'实线：已发生的流转':item.decision==='discard'?'本次暂不采用 · 来源提交保留':'虚线：尚未执行的去向'}</div>`;
}
function flowPage(item){
  if(item.phase==='route')return itemPage(item);
  row('进入当前节点',phaseNames[item.phase],()=>openNode(item.workflow.current,item));
  if(!item.result&&item.status!=='done')row('调整后续流程','去向与自动推进',()=>go('plan'));
  return issueContext(item)+repositoryMap(item)+`<div class="intro">${esc(statusText(item))}</div>`+rowHTML();
}
function graphPage(){
  const layout=commitLayout(data.graph,data.release_head),palette=['#3fb950','#58a6ff','#d29922','#bc8cff','#e67e22'];
  const width=Math.max(650,150+layout.commits.length*72),height=Math.max(220,70+layout.lanes.length*55);
  layout.commits.forEach(c=>{c.x=140+c.index*72;c.y=55+c.lane*55;});
  let svg=`<svg width="${width}" height="${height}" aria-label="横向 Git 提交图">`;
  layout.lanes.forEach(l=>svg+=`<text x="0" y="${59+l.lane*55}" class="graph-label">${esc(l.label.length>14?l.label.slice(0,12)+'…':l.label)}</text>`);
  layout.edges.forEach(e=>svg+=`<path d="${roundedCommitEdge(e.parent,e.child,e.slot)}" stroke="${palette[(e.slot?e.parent.lane:e.child.lane)%palette.length]}" class="graph-edge"/>`);
  layout.commits.forEach(c=>svg+=`<circle cx="${c.x}" cy="${c.y}" r="4" fill="${palette[c.lane%palette.length]}"/><text x="${c.x-24}" y="${c.y+20}" class="graph-title">${short(c.oid)}</text>`);
  svg+='</svg>';
  data.graph.forEach(c=>row(c.subject,short(c.oid),()=>notify(c.oid),c.refs.join(' · ')||c.author));
  return '<div class="context">项目 Git 历史 · 旧 → 新</div><div class="graph-wrap">'+svg+'</div>'+rowHTML();
}
function helpPage(){return `<div class="intro">选中事项，展开小流程，再进入节点处理</div>${facts([['↑ ↓','切换事项或当前页的选中条'],['← →','在事项列表中选择流程节点'],['Enter','进入页头、打开节点/操作或保存输入'],['Esc','返回上一级；不会取消后台任务'],['/','打开当前事项的固定动作'],['Shift+Enter','在蓝色输入栏中换行']])}<div class="section">可体验的路径</div><p class="text">传入贡献：准备 → 检查失败 → 选择 Workspace → 填写交接 → 实际创建工作分支。\n本地产出：准备 → 检查通过 → 正式草稿 → 实际写入 Release。\n\n小流程来自后端结果；未来节点只能查看前置条件。\n/plan 调整自动推进与去向。/pause 暂停后续动作。\n/flow、/changes、/discussion 分别打开独立页面。\n\n本演示使用隔离的真实 Git 与测试；讨论为本地记录，未接入 Agent。</p>`;}

function commands(){
  const item=current(), list=[];
  const add=(cmd,label,run,disabled=false)=>list.push({cmd,label,run,disabled});
  if(form)add('save','保存当前填写内容',submitForm);
  if(item){
    add('current','进入当前流程节点',()=>openNode(item.workflow.current,item));
    add('continue','继续当前步骤',()=>item.status==='paused'?act('resume'):['prepare','check','apply','push'].includes(item.phase)?act('continue'):notify('请返回当前步骤，完成选择或填写。'),item.status==='done'||item.status==='running');
    add('pause','暂停后续步骤',()=>act('pause'),item.status==='done');
    add('plan','调整后续流程',()=>go('plan'));
    add('diff','查看当前差异',()=>go('diff'));
    add('checks','查看检查结果',()=>go('checks'));
    add('flow','查看三仓流转',()=>go('flow'));
    add('discussion','查看或补充本地讨论',()=>go('discussion'));
    add('history','查看本次处理记录',()=>go('history'));
  }
  add('changes','打开 Git 历史图',()=>go('graph'));
  add('settings','提交格式、隐私、远端',()=>go('settings',null));
  add('items','返回事项列表',home);
  add('back','返回上一级',back);
  add('help','键盘操作与演示范围',()=>go('help'));
  add('reset','新建一组示例；旧数据保留',()=>go('reset',null));
  return list;
}
function openCommands(){
  if(ui.focus==='header'){ui.focus='content';}
  if(!commandOpen){saveDraft();commandReturn={...ui};commandOpen=true;ui.row=0;}
  ui.focus='command';render();$('command-input').focus();
}
function closeCommands(){if(commandReturn)ui={...commandReturn};commandReturn=null;commandOpen=false;$('command-input').value='';render();if(form)$('normal-input').focus();else $('terminal').focus();}
function commandPage(){
  const q=$('command-input').value.replace(/^\//,'').trim().toLowerCase();
  commandRows=commands().filter(c=>c.cmd.includes(q)||c.label.includes(q));
  commandRows.forEach(c=>row('/'+c.cmd,c.label,()=>executeCommand(c),c.disabled?'当前状态下不可执行。':'',c.disabled));
  return `<div class="context">${current()?esc(current().title)+' · ':''}固定动作</div>`+rowHTML();
}
function executeCommand(c){if(c.disabled){notify('当前状态下不可执行');return;}closeCommands();c.run();}

function render(){
  if(!data)return;
  const scroll=$('surface').scrollTop;rows=[];
  const item=current();let html='',title='事项';
  if(commandOpen){title='操作';html=commandPage();}
  else switch(ui.scene){
    case 'inbox':html=inbox();break;
    case 'node':title=item.workflow.steps.find(s=>s.key===ui.node)?.label||'流程节点';html=nodePage(item);break;
    case 'item':title=phaseNames[item.phase];html=itemPage(item);break;
    case 'plan':title='调整流程';html=planPage(item);break;
    case 'checks':title='检查结果';html=checksPage(item);break;
    case 'diff':title='当前差异';html=diffPage(item);break;
    case 'discussion':title='讨论记录';html=discussionPage(item);break;
    case 'history':title='处理记录';html=issueContext(item)+events(item,100);break;
    case 'flow':title='三仓流转';html=flowPage(item);break;
    case 'graph':title='Git 历史';html=graphPage();break;
    case 'settings':title='设置';html=settingsPage();break;
    case 'format':title='提交格式';html=formatPage();break;
    case 'privacy':title='隐私';html=privacyPage();break;
    case 'remote':title='远端';html=remotePage();break;
    case 'form':title=form?.label||'填写';html=formPage(item);break;
    case 'help':title='操作说明';html=helpPage();break;
    case 'reset':title='新建示例';row('新建一组示例','旧示例保留',async()=>{if(await act('reset',{},null)){drafts.clear();nodeChoices.clear();home();}});row('返回','',back);html='<div class="intro">当前示例保留在本地；界面切换到一组新的待处理事项。</div>'+rowHTML();break;
  }
  $('header').innerHTML=`<button aria-label="进入${esc(title)}">${esc(title)}</button>`;
  $('header').classList.toggle('entered',ui.focus!=='header');
  $('surface').innerHTML=html;$('surface').scrollTop=scroll;
  $('surface').classList.toggle('inactive',ui.focus==='header');
  $('command-bar').hidden=ui.focus==='header';
  $('normal-bar').hidden=!form||commandOpen||ui.focus==='header';
  $('input-label').textContent=form?.label||'';
  $('command-input').placeholder=form?'save 保存 · back 返回 · / 查看动作':'输入 / 查看当前可用动作';
  $('keys').textContent=ui.focus==='header'?'Enter 进入菜单':commandOpen?'↑ ↓ 选择动作　Enter 执行　Esc 返回':form?'Enter 保存　Shift+Enter 换行　Esc 返回　Ctrl+/ 命令':ui.scene==='inbox'?'↑ ↓ 事项　← → 节点　Enter 处理　Esc 页头　/ 操作':'↑ ↓ 选择　Enter 打开　Esc 返回　/ 操作';
  notify(notice,noticeError);
  $('header').querySelector('button').onclick=()=>{ui.focus='content';render();$('terminal').focus();};
  document.querySelectorAll('[data-row]').forEach(el=>{el.onclick=()=>{ui.focus=commandOpen?'command':'content';ui.row=Number(el.dataset.row);render();$('terminal').focus();};el.ondblclick=activate;});
  document.querySelectorAll('[data-node]').forEach(el=>{el.onclick=()=>{nodeChoices.set(el.dataset.item,el.dataset.node);render();$('terminal').focus();};el.ondblclick=()=>openNode(el.dataset.node,current());});
}
function activate(){const selected=rows[ui.row];if(!selected)return;if(selected.disabled){notify(selected.hint||'当前条件尚未满足');return;}selected.run();}
function move(delta){if(!rows.length){$('surface').scrollBy({top:delta*50});return;}ui.row=Math.max(0,Math.min(rows.length-1,ui.row+delta));render();document.querySelector(`[data-row="${ui.row}"]`)?.scrollIntoView({block:'nearest'});}

$('normal-input').addEventListener('input',saveDraft);
$('command-input').addEventListener('focus',()=>{if(!commandOpen)openCommands();});
$('command-input').addEventListener('input',()=>{if(!commandOpen)openCommands();ui.row=0;render();});
document.addEventListener('keydown',event=>{
  if(!data||event.isComposing)return;
  if(event.key==='Escape'){event.preventDefault();back();return;}
  if(event.key==='/'&&event.ctrlKey){event.preventDefault();openCommands();return;}
  if(event.target===$('normal-input')&&!commandOpen){
    if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();submitForm();}
    return;
  }
  if(ui.focus==='header'){
    if(event.key==='Enter'){event.preventDefault();ui.focus='content';render();$('terminal').focus();}
    else if(event.key==='/'){event.preventDefault();openCommands();}
    return;
  }
  if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();move(event.key==='ArrowDown'?1:-1);return;}
  if(!commandOpen&&ui.scene==='inbox'&&(event.key==='ArrowLeft'||event.key==='ArrowRight')){event.preventDefault();moveNode(event.key==='ArrowRight'?1:-1);return;}
  if(event.key==='Enter'){event.preventDefault();activate();return;}
  if(!commandOpen&&event.key==='/'){event.preventDefault();$('command-input').value='';openCommands();return;}
  if(event.key==='PageDown'||event.key==='PageUp'){event.preventDefault();$('surface').scrollBy({top:(event.key==='PageDown'?1:-1)*$('surface').clientHeight*.8});}
});
refresh().catch(e=>notify(e.message,true));
setInterval(()=>refresh().catch(e=>notify(e.message,true)),850);
