"""Isolated reference fixture. No real accounts, remote forge or product state."""
import copy
import io
import json
import subprocess
import uuid
import zipfile
from pathlib import Path

BROKEN_SOURCE = "def delivery_manifest(version):\n    return {'version': version.strip(), 'schema': 1}\n"
FIXED_SOURCE = "def delivery_manifest(version):\n    value = version.strip()\n    if not value:\n        raise ValueError('version is required')\n    return {'version': value, 'schema': 1}\n"
TEST_SOURCE = "import unittest\nfrom delivery_manifest import delivery_manifest\nclass Delivery(unittest.TestCase):\n def test_manifest(self): self.assertEqual(delivery_manifest(' 0.9.0 ')['version'], '0.9.0')\n def test_blank(self):\n  with self.assertRaises(ValueError): delivery_manifest('  ')\n"

class ThreeRepoWorkflow:
    def flow_repo(self, role):
        self.require(role in ('workspace','trial','release'),'BAD_ROLE','仓库角色无效',422)
        return self.repo if role=='release' else self.root/self.s['session']/'workflow'/(role+'.git')

    def flow_tip(self, role):
        return self.git_at(self.flow_repo(role),'rev-parse','refs/heads/'+('master' if role=='release' else role)).stdout.decode().strip()

    def repo_files(self, repo, oid):
        with zipfile.ZipFile(io.BytesIO(self.git_at(repo,'archive','--format=zip',oid).stdout)) as z:
            return {name:z.read(name).decode() for name in z.namelist() if not name.endswith('/')}

    def flow_import(self, role, oid):
        self.git('fetch','--no-tags',str(self.flow_repo(role)),oid)
        self.git('update-ref','refs/demo/workflow/'+role,oid)

    def ensure_workflow(self):
        if 'workflow' in self.s: return
        baseline=self.oid('refs/heads/master')
        folder=self.root/self.s['session']/'workflow';folder.mkdir(exist_ok=True)
        for role in ('workspace','trial'):
            repo=self.flow_repo(role)
            if not repo.exists():
                r=subprocess.run(['git','init','--bare','--initial-branch='+role,'--template=',str(repo)],capture_output=True)
                self.require(r.returncode==0,'FLOW_INIT_FAILED','初始化隔离仓库失败',500)
            self.git_at(repo,'fetch','--no-tags',str(self.repo),baseline)
        files=self.files(baseline)
        w=self.make_commit(self.flow_repo('workspace'),{**files,'delivery_manifest.py':BROKEN_SOURCE,'test_delivery_manifest.py':TEST_SOURCE},[baseline],'feat: generate delivery manifest [W1]','Lin')
        t=self.make_commit(self.flow_repo('trial'),{**files,'docs/reviewer-notes.md':'# Review\nConfirm version normalization and blank-version rejection.\n'},[baseline],'docs: record integration acceptance criteria [T1]','Chen')
        self.git_at(self.flow_repo('workspace'),'update-ref','refs/heads/workspace',w)
        self.git_at(self.flow_repo('trial'),'update-ref','refs/heads/trial',t)
        self.flow_import('workspace',w);self.flow_import('trial',t)
        iid=max(i['id'] for i in self.s['issues'])+1
        self.s['issues'].append({'id':iid,'title':'交付清单：三仓验收','body':'Workspace 产出清单；Trial 检查版本规范化与空版本边界；Release 接纳后推送并构建。','author':'lin','owner':'chen','state':'open','version':1,'comments':[]})
        self.s['workflow']={'baseline':baseline,'issue':iid,'scan':None,'transfer':None,'candidate':None,'tested':None,'check':None,'formal':None,'synced':None,'pushed':None,'build':None,'attempts':[],'error':None,'edit_count':0}

    def flow_snapshot(self):
        f=copy.deepcopy(self.s['workflow'])
        f['repositories']={role:{'role':role,'path':str(self.flow_repo(role)),'ref':'refs/heads/'+('master' if role=='release' else role),'head':self.flow_tip(role)} for role in ('workspace','trial','release')}
        w=f['repositories']['workspace']['head'];t=f['repositories']['trial']['head']
        current=bool(f['scan'] and f['scan']['head']==w)
        candidate_current=bool(current and f['candidate'] and f.get('submitted_head')==w and f['candidate']==t)
        tested=bool(candidate_current and f['tested']==t and f['check'] and f['check']['state']=='passed')
        formal=next((x for x in self.s['formal_commits'] if x['id']==f['formal'] and x['state'] not in ('deleted','dissolved')),None)
        release=next((r for r in reversed(self.s['releases']) if r['build']==f['build']),None)
        f['release']=release['id'] if release else None
        distributed=bool(release and all(x['state']=='published' for x in release['destinations'].values()))
        complete=[current,candidate_current,tested,bool(formal),bool(f['synced']),bool(f['pushed']),bool(f['build']),distributed]
        labels=['比较与选取','送入 Trial','组合验证','整理正式提交','同步 Release','推送 Git 远端','构建产物','分发版本']
        actions=['scan','submit','check','formalize','sync','push','build','distribution']
        first=next((i for i,done in enumerate(complete) if not done),len(complete))
        f['steps']=[{'id':actions[i],'label':label,'state':'done' if complete[i] else 'current' if i==first else 'pending'} for i,label in enumerate(labels)]
        f['completed']=sum(complete);f['current']=actions[first] if first<len(actions) else 'done'
        f['current_label']=labels[first] if first<len(labels) else '交付完成'
        f['blocked']=bool(candidate_current and f['check'] and f['check']['state']=='failed')
        if f['blocked']:f['current_label']='Trial 验证失败 · 返回 Workspace 修订'
        f['workspace_body']=self.repo_files(self.flow_repo('workspace'),w).get('delivery_manifest.py','')
        f['suggested_body']=FIXED_SOURCE
        return f

    def workflow_action(self, actor, action, a):
        self.maintainer(actor)
        self.ensure_workflow();f=self.s['workflow'];step=action.split('.',1)[1]
        before={role:self.flow_tip(role) for role in ('workspace','trial','release')}
        self.require(step in ('scan','select','submit','check','promote','discard','edit','formalize','sync','push','build'),'BAD_FLOW_ACTION','流程动作无效',422)
        try:
            result=self.workflow_execute(actor,step,a,before)
            failed=step=='check' and result['state']=='failed'
            f['error']={'step':step,'message':'组合测试未通过，Release 未改变。','details':result} if failed else None
            rec={'step':step,'state':'failed' if failed else 'done','before':before,'after':{role:self.flow_tip(role) for role in before},'result':result}
        except Exception as error:
            if not hasattr(error,'code'):raise
            f['error']={'step':step,'code':error.code,'message':str(error),'details':error.details}
            rec={'step':step,'state':'blocked','before':before,'after':{role:self.flow_tip(role) for role in before},'error':f['error']}
        f['attempts'].append(rec);self.event(actor,action,step+' · '+rec['state'],rec)
        return rec

    def workflow_execute(self, actor, step, a, before):
        f=self.s['workflow'];wr=self.flow_repo('workspace');tr=self.flow_repo('trial')
        if step in ('select','submit','promote','discard','edit','formalize'):
            self.require(not f['synced'],'ROUND_SYNCED','该轮已同步；保留此轮交付证据。新工作另开候选。')
        if step=='scan':
            if f['scan'] and f['scan']['head']==before['workspace'] and f.get('submitted_head')==before['workspace']:
                return f['scan']
            names=self.git_at(wr,'diff','--name-status',f['baseline'],before['workspace']).stdout.decode().splitlines()
            f['scan']={'head':before['workspace'],'base':f['baseline'],'files':[{'status':line.split('\t')[0],'path':line.split('\t')[-1],'selected':True} for line in names]}
            return f['scan']
        if step=='select':
            self.require(f['scan'] and f['scan']['head']==before['workspace'],'SCAN_STALE','先比较当前工作快照')
            self.require(not f['candidate'] or f.get('submitted_head')!=before['workspace'],'ALREADY_TRANSFERRED','当前候选已送入 Trial；新修订重新扫描时调整范围')
            wanted=set(a['paths']);valid={x['path'] for x in f['scan']['files']}
            self.require(wanted and wanted<=valid,'BAD_SELECTION','至少选择一个实际变更路径',422)
            for entry in f['scan']['files']:entry['selected']=entry['path'] in wanted
            f.update(candidate=None,tested=None,check=None,formal=None)
            return f['scan']
        if step=='submit':
            scan=f['scan'];self.require(scan and scan['head']==before['workspace'],'SCAN_STALE','Workspace 已变化，请重新比较')
            self.require(a.get('expected_workspace')==before['workspace'] and a.get('expected_trial')==before['trial'],'FLOW_MOVED','仓库引用已变化，请重新预览')
            files=self.repo_files(wr,f['baseline']);current=self.repo_files(wr,before['workspace'])
            for item in scan['files']:
                if item['selected']:
                    if item['path'] in current:files[item['path']]=current[item['path']]
                    else:files.pop(item['path'],None)
            transfer=self.make_commit(wr,files,[f['transfer'] or f['baseline']],'chore: selected workspace snapshot')
            self.git_at(wr,'update-ref','refs/demo/selected',transfer)
            self.git_at(tr,'fetch','--no-tags',str(wr),transfer)
            merged=self.git_at(tr,'merge-tree','--write-tree',before['trial'],transfer,check=False)
            self.require(merged.returncode==0,'MERGE_CONFLICT','Trial 有冲突；原目标保留，需回 Workspace 解决',409)
            tree=merged.stdout.decode().splitlines()[0]
            candidate=self.git_at(tr,'commit-tree',tree,'-p',before['trial'],'-p',transfer,data=b'integrate: workspace snapshot into Trial').stdout.decode().strip()
            self.git_at(tr,'update-ref','refs/heads/trial',candidate,before['trial'])
            self.flow_import('trial',candidate)
            f.update(transfer=transfer,candidate=candidate,submitted_head=before['workspace'],tested=None,check=None,formal=None)
            p=next((p for p in self.s['proposals'] if p['id']=='workflow'),None)
            if not p:
                p={'id':'workflow','number':99,'title':'交付清单 · 三仓候选','author':'lin','branch':'workflow/trial','base':f['baseline'],'issue':f['issue'],'review':'pending','history':[],'integrations':[]};self.s['proposals'].append(p)
            p.update(candidate=candidate,label='T'+str(len(p['history'])+2));p['history'].append({'label':p['label'],'oid':candidate})
            return {'source':before['workspace'],'selected_snapshot':transfer,'trial_candidate':candidate,'file_count':sum(x['selected'] for x in scan['files'])}
        if step=='check':
            self.require(f['candidate']==before['trial'] and f.get('submitted_head')==before['workspace'],'CANDIDATE_STALE','先将当前 Workspace 快照送入 Trial')
            check=self.check(f['candidate']);privacy,_=self.outbound_view(f['candidate'])
            if privacy['blocked']:check['state']='failed'
            check['privacy']=privacy;f.update(tested=f['candidate'],check=check)
            self.s['checks'][f['candidate']]=check
            return check
        if step=='promote':
            self.require(f['candidate']==before['trial'],'NO_CANDIDATE','Trial 尚无可返回的候选')
            self.git_at(wr,'fetch','--no-tags',str(tr),f['candidate'])
            self.git_at(wr,'update-ref','refs/heads/workspace',f['candidate'],before['workspace'])
            self.flow_import('workspace',f['candidate']);f['scan']=None
            return {'source':f['candidate'],'workspace':f['candidate'],'release_unchanged':before['release']}
        if step=='discard':
            self.require(f['candidate']==before['trial'],'NO_CANDIDATE','当前 Trial 没有可撤回的候选')
            self.require(a.get('expected_trial')==before['trial'],'FLOW_MOVED','Trial 已变化，请重新预览')
            first=next(r for r in f['attempts'] if r['step']=='submit' and r['state']=='done')
            archive='refs/archived/'+before['trial']
            self.git_at(tr,'update-ref',archive,before['trial'])
            self.git_at(tr,'update-ref','refs/heads/trial',first['before']['trial'],before['trial'])
            self.flow_import('trial',first['before']['trial'])
            self.s['triage'][before['trial']]={'source':before['trial'],'decision':'discard','proposal':'workflow'}
            for record in self.s['formal_commits']:
                if record['id']==f['formal'] and record['state']=='local':record['state']='dissolved'
            f.update(scan=None,transfer=None,candidate=None,tested=None,check=None,formal=None)
            return {'archived':archive,'trial':first['before']['trial'],'workspace_unchanged':before['workspace'],'release_unchanged':before['release']}
        if step=='edit':
            self.require(a.get('expected_workspace')==before['workspace'],'FLOW_MOVED','Workspace 已变化，保留输入并刷新')
            body=str(a.get('body',''));self.require(0<len(body)<=16000,'BAD_TEXT','请输入 1–16000 字符的示例文件内容',422)
            files=self.repo_files(wr,before['workspace']);files['delivery_manifest.py']=body
            f['edit_count']+=1
            head=self.make_commit(wr,files,[before['workspace']],f"fix: revise delivery manifest [W{f['edit_count']+1}]",'Lin')
            self.git_at(wr,'update-ref','refs/heads/workspace',head,before['workspace']);self.flow_import('workspace',head)
            f.update(scan=None,candidate=None,tested=None,check=None,formal=None)
            return {'workspace':head,'changed':'delivery_manifest.py'}
        if step=='formalize':
            self.require(self.flow_snapshot()['steps'][2]['state']=='done','CHECKS_REQUIRED','当前候选组合验证必须通过')
            self.require(not any(x['id']==f['formal'] and x['state'] not in ('deleted','dissolved') for x in self.s['formal_commits']),'ALREADY_PREPARED','正式草稿已存在')
            result=self.publish_action(actor,'formal.prepare',{'source':f['candidate']});f['formal']=result['id'];return result
        if step=='sync':
            self.require(f['formal'],'FORMAL_REQUIRED','先整理正式提交')
            self.require(a.get('expected_release')==before['release'],'FLOW_MOVED','Release 已变化，请重新预览')
            result=self.publish_action(actor,'formal.sync',{'formal':f['formal'],'expected_target':before['release']});f['synced']=result['result'];return result
        if step=='push':
            self.require(f['synced']==before['release'],'SYNC_REQUIRED','先确认当前 Release 同步结果')
            result=self.publish_action(actor,'remote.push',{'source':f['synced']});f['pushed']=result['remote_tip'];return result
        if step=='build':
            self.require(f['synced']==before['release'],'SYNC_REQUIRED','先同步正式结果')
            result=self.build(actor,{'source':f['synced']});f['build']=result['id'];return result
