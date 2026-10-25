"""Isolated reference fixture. No real accounts, remote forge or product state."""
import copy
import re
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse
import source_helpers as helpers

class PublishFeatures:
    def ensure_publish(self):
        if 'publish' in self.s: return
        self.s['publish']={
            'commit_format':{**helpers.CFG['DEFAULT_COMMIT_FORMAT'],'prefix':self.s['config'].get('prefix','GG')},
            'privacy':helpers.default_policy(),
            'remote_url':str(self.root/self.s['session']/'release-remote.git'),
            'templates':{'default':{'header_format':helpers.FORMAT['_DEFAULT_HEADER'],'body_format':helpers.FORMAT['_DEFAULT_BODY'],'prefix_override':None}},
            'scan':None}
        self.s['formal_commits']=[]
        self.s['triage']={}
        self.s['workspace_sources']=[]

    def formatted(self, source):
        self.require(isinstance(source,str) and re.fullmatch(r'[0-9a-f]{40}',source),'BAD_SOURCE','需要固定的 Git OID',422)
        config=self.s['publish']['commit_format']
        self.require(not config['plugins'],'PLUGIN_UNAVAILABLE','样机未装载这些提交插件；请清空插件列表或在正式 Host 验证，不能假装钩子执行成功。',422)
        self.require(config['template_name'] in self.s['publish']['templates'],'TEMPLATE_MISSING','模板不存在，请选择 default 或创建本地演示模板。',422)
        return helpers.format_message(self,source,config,self.s['publish']['templates'])

    def outbound_view(self, source, files=None):
        policy=self.s['publish']['privacy']
        included,excluded,findings={},[],[]
        for name,body in (self.files(source) if files is None else files).items():
            hard=helpers.private(name,body)
            explicit=any(helpers.match(p,name) for p in policy['include_paths'])
            excluded_by_rule=any(helpers.match(p,name) for p in policy['exclude_paths'])
            if hard or (excluded_by_rule and not explicit):
                excluded.append({'path':name,'reason':'私有工具/凭据配置' if hard else '路径排除规则'})
                continue
            included[name]=body
            findings.extend({'path':name,**item} for item in helpers.scan(name,body,policy))
        report={'source':source,'policy_version':policy['version'],'included':list(included),
                'excluded':excluded,'findings':findings,'blocked':any(x['level']=='error' for x in findings)}
        return report,included

    def require_outbound(self, source, files=None, already_packaged=False):
        report,allowed=self.outbound_view(source,files)
        self.require(not report['blocked'],'PRIVACY_BLOCKED','出站检查发现需要处理的内容。查看 Privacy 扫描结果；未执行发布。',422,report)
        self.require(not already_packaged or not report['excluded'],'PACKAGE_POLICY_CHANGED','现有包包含当前策略排除的文件，请重新构建，不能只改页面状态。',422,report)
        return report,allowed

    def formal_record(self, source, message, *, state='local', result=None):
        tag=re.match(r'^\[([A-Z0-9]+)-(\d+)\]',message)
        self.require(tag,'INVALID_MESSAGE','正式提交首行需要 [PREFIX-N] type: subject',422)
        self.require(not any(f['prefix']==tag[1] and f['number']==int(tag[2]) for f in self.s['formal_commits']),
                     'NUMBER_RESERVED','该正式提交编号已使用，请重新生成消息。',409)
        record={'id':'formal-'+uuid.uuid4().hex[:10],'source':source,'message':message,
                'prefix':tag[1],'number':int(tag[2]),'state':state,'result':result,
                'synced':state in ('synced','pushed'),'pushed':state=='pushed'}
        self.s['formal_commits'].append(record)
        return record

    def publish_action(self, actor, action, a):
        self.ensure_publish()
        if action=='publish.set':
            self.maintainer(actor)
            section,value=a['section'],a['value']
            if section=='commit_format':
                self.require(isinstance(value,dict) and not(set(value)-set(helpers.CFG['DEFAULT_COMMIT_FORMAT'])),'INVALID_COMMIT_FORMAT','提交格式字段无效',422)
                value={**self.s['publish'][section],**value}
                self.require(re.fullmatch('[A-Z0-9]+',str(value['prefix'])),'INVALID_PREFIX','Prefix 需要大写字母或数字',422)
                self.require(type(value['number_start']) is int and 0<=value['number_start']<=9999,'INVALID_NUMBER','Number Start 需要 0–9999 的整数',422)
                self.require(isinstance(value['plugins'],list) and type(value['padding']) is bool,'INVALID_FORMAT','Plugins 需要列表，Padding 需要布尔值',422)
                self.require(value['template_name'] in self.s['publish']['templates'],'TEMPLATE_MISSING','请先创建该模板',422)
                self.s['config']['prefix']=value['prefix']
            elif section=='privacy':
                value=helpers.normalize_policy(value)
                value['version']=self.s['publish'][section]['version']+1
                self.s['config']['privacy']=value['enabled']
            elif section=='remote_url':
                value=str(value).strip(); parsed=urlparse(value)
                self.require(not parsed.password and not(parsed.scheme in ('http','https') and parsed.username),'CREDENTIAL_IN_URL','远端地址不要包含凭据',422)
                self.require(not value or (not value.startswith('-') and not any(ord(c)<32 for c in value) and (Path(value).is_absolute() or parsed.scheme in ('http','https','ssh','git','file') or re.fullmatch(r'[^@\s]+@[^:\s]+:[^\s]+',value))),'INVALID_REMOTE_URL','需要绝对 Git URL、SSH 地址或本地绝对路径',422)
            else: self.require(False,'INVALID_SECTION','未知 Publish 配置段',422)
            self.s['publish'][section]=value
            self.event(actor,action,'已保存 '+section)
            return {'section':section,'value':value}
        if action=='format.preview':
            return {'source':a['source'],'message':self.formatted(a['source']),
                    'padding_effective':False,'note':'当前 Gitgo build_commit_template 未消费 padding；保留配置，不虚构行为。'}
        if action=='template.save':
            self.maintainer(actor); name=str(a['name']).strip()
            self.require(re.fullmatch(r'[\w-]{1,40}',name) and name!='default','BAD_TEMPLATE','请使用非 default 的模板名称',422)
            value={k:a.get(k) for k in ('header_format','body_format')};value['prefix_override']=None
            try:
                value['header_format'].format(prefix='GG',number=1,type_str='feat',scope_str='',subject='demo',project_name='atlas',commit_count='1',commit_list='demo')
                value['body_format'].format(prefix='GG',number=1,type_str='feat',scope_str='',subject='demo',project_name='atlas',commit_count='1',commit_list='demo')
            except (KeyError,ValueError,AttributeError,IndexError): self.require(False,'BAD_TEMPLATE','模板变量或格式无效',422)
            self.s['publish']['templates'][name]=value
            self.event(actor,action,'已保存本地演示模板 '+name)
            return {'name':name,**value}
        if action=='privacy.scan':
            self.writer(actor)
            source=a.get('source') or self.oid('refs/heads/master')
            files=self.files(source)
            if a.get('sample'):
                files={**files,'.env':'DEMO=private configuration\n','docs/draft.md':'internal draft\n',
                       'demo-risk.txt':'Provider fixture: '+'sk-'+'x'*32+'\n'}
            report,_=self.outbound_view(source,files)
            report['sample']=bool(a.get('sample'))
            self.s['publish']['scan']=report
            self.event(actor,action,'出站扫描：'+('阻止' if report['blocked'] else '通过'),report)
            return report
        if action=='formal.prepare':
            self.maintainer(actor)
            source=a['source'];message=a.get('message') or self.formatted(source)
            self.require(any(p['candidate']==source for p in self.s['proposals']),'STALE_CANDIDATE','请选择当前候选',422)
            f=self.formal_record(source,message)
            self.event(actor,action,'已整理为本地正式提交草稿',{'formal':f['id'],'source':source})
            return f
        if action.startswith('formal.'):
            self.maintainer(actor)
            f=next((f for f in self.s['formal_commits'] if f['id']==a['formal']),None)
            self.require(f,'NOT_FOUND','正式提交不存在',404)
            self.require(f['state']=='local','ALREADY_SYNCED','该提交已同步；不能通过编辑、删除或解散重写已发布历史。请创建修复或 revert 候选。')
            if action=='formal.edit':
                self.require(re.match(r'^\[[A-Z0-9]+-\d+\]\s+\w+',a['message']),'INVALID_MESSAGE','提交格式无效',422)
                # Keep allocated identity fixed while editing content.
                self.require(a['message'].startswith(f"[{f['prefix']}-{f['number']}]"),'IDENTITY_CHANGED','编辑内容时保留已分配的提交编号',422)
                f['message']=a['message']
            elif action in ('formal.delete','formal.dissolve'):
                f['state']='deleted' if action.endswith('delete') else 'dissolved'
                if action.endswith('dissolve'): self.s['workspace_sources'].append(f['source'])
            elif action=='formal.sync':
                p=next((p for p in self.s['proposals'] if p['candidate']==f['source']),None)
                self.require(p,'STALE_CANDIDATE','来源已有新候选，保留草稿并重新整理')
                plan=self.integration_preview(actor,{'proposal':p['id'],'source':f['source'],'target':'master','method':'squash','owner':actor})
                self.require(a.get('expected_target')==plan['expected_target'],'TARGET_MOVED','正式目标变化，请重新预览')
                result=self.integrate(actor,{**plan,'message':f['message'],'formal_id':f['id']})
                f.update(state='synced',synced=True,result=result['result'])
            else:self.require(False,'UNKNOWN_COMMAND','未知 Formal 动作',404)
            self.event(actor,action,'正式提交状态：'+f['state'],{'formal':f['id']})
            return f
        if action=='trial.triage':
            self.maintainer(actor);p=self.proposal(a['proposal'])
            self.require(p['candidate']==a['source'],'STALE_CANDIDATE','候选已更新')
            decision=a['decision']
            self.require(decision in ('accept','promote','discard'),'BAD_TRIAGE','三叉动作无效',422)
            if decision=='accept':
                self.require(False,'PLAN_REQUIRED','Accept 请打开整合预览，明确正式目标与方法后执行',422)
            result={'source':p['candidate'],'decision':decision,'proposal':p['id']}
            if decision=='promote':
                branch=self.branch_name(a.get('branch') or 'incoming/'+p['candidate'][:8])
                self.require(not self.oid('refs/heads/'+branch),'BRANCH_EXISTS','工作分支已存在')
                self.git('update-ref','refs/heads/'+branch,p['candidate'],'0'*40)
                self.s['branches'][branch]={'owner':a.get('owner',actor),'purpose':'Workspace','retention':'继续加工','deleted':False}
                result['branch']=branch
            self.s['triage'][p['candidate']]=result
            self.event(actor,action,'Trial '+decision,result)
            return result
        if action=='remote.push':
            self.maintainer(actor)
            source=self.oid('refs/heads/master')
            self.require(source==a['source'],'TARGET_MOVED','正式线变化，请重新预览')
            target=(self.root/self.s['session']/'release-remote.git').resolve()
            self.require(str(target)==self.s['publish']['remote_url'],'EXTERNAL_REMOTE_UNAVAILABLE','此演示只向本地隔离 remote 推送；外部 URL 保留配置，由正式平台适配器执行。',422)
            report,_=self.require_outbound(source,already_packaged=True)
            if not target.exists():
                import subprocess
                r=subprocess.run(['git','init','--bare','--initial-branch=master','--template=',str(target)],capture_output=True)
                self.require(r.returncode==0,'INIT_FAILED','创建演示远端失败',500)
            old=self.git_at(target,'rev-parse','--verify','refs/heads/master',check=False)
            old_oid=old.stdout.decode().strip() if old.returncode==0 else None
            # Every outgoing commit is inspected, including secrets deleted in later commits.
            range_arg=f'{old_oid}..{source}' if old_oid else source
            for oid in self.text('rev-list',range_arg).splitlines(): self.require_outbound(oid,already_packaged=True)
            self.git('push',str(target),f'{source}:refs/heads/master')
            actual=self.git_at(target,'rev-parse','refs/heads/master').stdout.decode().strip()
            self.require(actual==source,'OUTCOME_UNKNOWN','推送结果需要核对',409)
            for f in self.s['formal_commits']:
                if f.get('result') and self.git('merge-base','--is-ancestor',f['result'],source,check=False).returncode==0:
                    f.update(state='pushed',synced=True,pushed=True)
            result={'source':source,'remote_tip':actual,'remote_url':str(target),'policy_version':report['policy_version']}
            self.event(actor,action,'Release Git 远端已确认',result)
            return result
        self.require(False,'UNKNOWN_COMMAND','未知 Publish 动作',404)
