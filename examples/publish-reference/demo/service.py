"""Publish handoff reference; not production implementation or authentication."""
from __future__ import annotations
import argparse
import copy
import importlib.util
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PREVIOUS = ROOT / '_fixture'
sys.path.insert(0, str(PREVIOUS))
spec = importlib.util.spec_from_file_location('publish_fixture', PREVIOUS / 'server.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
Problem, require, now = fixture.Problem, fixture.require, fixture.now


class Flow(fixture.Demo):
    def __init__(self, root):
        super().__init__(root)
        self.due = {}
        # Restart never blindly repeats a possibly completed external effect.
        for item in self.items:
            if item['status'] == 'running':
                item.update(status='paused', error='演示服务重新启动；核对当前结果后继续。')
        self.save()

    @property
    def items(self):
        return self.s['terminal_flow']['items']

    def ensure_workflow(self):
        if 'terminal_flow' in self.s:
            return
        baseline = self.oid('refs/heads/master')
        for role in ('workspace', 'trial'):
            path = self.flow_repo(role)
            path.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(['git', 'init', '--bare', '--template=', '--initial-branch='+role, str(path)], capture_output=True)
            require(result.returncode == 0, 'INIT', '创建隔离仓库失败')
            self.git_at(path, 'fetch', '--no-tags', str(self.repo), baseline)
            self.git_at(path, 'update-ref', 'refs/heads/'+role, baseline)
        self.s['terminal_flow'] = {'items': [], 'events': [], 'schema': 1}
        for pid, role, title in [('p17', 'trial', '离线下载与文件名归一化'), ('p18', 'workspace', '安装包 SHA-256 校验')]:
            proposal = self.proposal(pid)
            branch = 'incoming/17' if role == 'trial' else 'feature/checksum'
            self.git_at(self.flow_repo(role), 'fetch', '--no-tags', str(self.repo), proposal['candidate'])
            self.git_at(self.flow_repo(role), 'update-ref', 'refs/heads/'+branch, proposal['candidate'])
            self.items.append({'id': pid, 'version': 1, 'kind': 'incoming' if role == 'trial' else 'local',
                'title': title, 'author': 'Lin' if role == 'trial' else 'Duo', 'source': proposal['candidate'],
                'source_role': role, 'source_ref': branch, 'base': baseline, 'label': proposal['label'],
                'phase': 'prepare', 'status': 'ready', 'evidence': None, 'check': None, 'result': None,
                'error': '', 'note': '', 'message': '', 'formal_id': None, 'decision': None,
                'plan': {'auto_prepare': True, 'auto_apply': False, 'target': 'workspace' if role == 'trial' else 'release',
                         'branch': 'incoming/offline-17' if role == 'trial' else 'master', 'push': False},
                'events': [], 'notes': [], 'history': [{'oid': proposal['candidate'], 'label': proposal['label']}],
                'expected_target': None, 'pushed': None})

    def item(self, item_id):
        item = next((i for i in self.items if i['id'] == item_id), None)
        require(item, 'NOT_FOUND', '事项不存在', 404)
        return item

    def record(self, item, action, text, details=None):
        event = {'time': now(), 'action': action, 'text': text, 'details': details}
        item['events'].append(event)
        self.s['terminal_flow']['events'].append({'item': item['id'], **event})

    def target_tip(self, item):
        role, branch = item['plan']['target'], item['plan']['branch']
        result = self.git_at(self.flow_repo(role), 'rev-parse', '--verify', 'refs/heads/'+branch, check=False)
        return result.stdout.decode().strip() if result.returncode == 0 else None

    def view(self):
        with self.lock:
            items = copy.deepcopy(self.items)
            for item in items:
                item['workflow'] = self.workflow_view(item)
            return {'schema': 1, 'revision': self.s['revision'], 'project': 'atlas',
                'items': items, 'settings': copy.deepcopy(self.s['publish']),
                'graph': self.graph(), 'release_head': self.oid('refs/heads/master'),
                'events': copy.deepcopy(self.s['terminal_flow']['events']),
                'scope': {'git': 'real-isolated', 'checks': 'real-fixture-tests', 'agent': 'not-connected', 'comments': 'local-only'}}

    def workflow_view(self, item):
        """Read-only projection of recorded outcomes, not an execution authority."""
        current = {'handoff': 'write', 'compose': 'write'}.get(item['phase'], item['phase'])
        discard = item['decision'] == 'discard'
        definitions = [('prepare', '准备'), ('check', '检查')]
        if item['kind'] == 'incoming':
            definitions.append(('route', '去向'))
        definitions += [('write', '交接' if item['plan']['target'] == 'workspace' else '草稿'), ('apply', '接纳')]
        if item['plan']['push'] and item['plan']['target'] == 'release':
            definitions.append(('push', '推送'))
        definitions.append(('done', '结果'))
        completed = {
            'prepare': bool(item['evidence']), 'check': bool(item['check']),
            'route': bool(item['decision']),
            'write': item['phase'] in ('apply', 'push', 'done') and not discard and
                (bool(item['formal_id']) if item['plan']['target'] == 'release' else
                 item['decision'] == 'workspace' and bool(item['note'])),
            'apply': bool(item['result']), 'push': bool(item['pushed']), 'done': item['status'] == 'done',
        }
        steps = []
        for key, label in definitions:
            active = key == current
            state = 'passed' if completed[key] else 'pending'
            if discard and key in ('write', 'apply', 'push'):
                state = 'skipped'
            elif key == 'check' and item['check'] and item['check']['state'] != 'passed':
                state = 'failed'
            elif active and item['status'] != 'done':
                state = {'ready': 'ready', 'awaiting': 'waiting', 'running': 'running',
                         'paused': 'paused', 'blocked': 'failed'}[item['status']]
            steps.append({'key': key, 'label': label, 'state': state, 'current': active})
        return {'current': current, 'steps': steps}

    def schedule(self, item):
        item.update(status='running', error='')
        self.due[item['id']] = time.monotonic() + 0.85

    def changed(self, item=None):
        if item:
            item['version'] += 1
        self.s['revision'] += 1
        self.save()

    def step(self, item):
        phase = item['phase']
        if phase == 'prepare':
            source = item['source']
            self.git('cat-file', '-e', source+'^{commit}')
            item['expected_target'] = self.target_tip(item)
            privacy, _ = self.outbound_view(source)
            diff = self.text('diff', '--no-ext-diff', '--no-textconv', '--stat', item['base'], source, '--')
            patch = self.text('diff', '--no-ext-diff', '--no-textconv', '--unified=3', item['base'], source, '--')
            paths = self.text('diff', '--name-only', item['base'], source, '--').splitlines()
            item['evidence'] = {'source': source, 'base': item['base'], 'target': item['expected_target'],
                'diff': patch, 'stat': diff, 'paths': paths, 'privacy': privacy, 'created': now()}
            item.update(phase='check', status='ready')
            self.record(item, 'prepared', f'已固定 {item["label"]} · {len(paths)} 个变更文件', {'source': source})
            if item['plan']['auto_prepare']:
                self.schedule(item)
        elif phase == 'check':
            item['check'] = self.check(item['source'])
            item['message'] = self.formatted(item['source'])
            passed = item['check']['state'] == 'passed'
            item.update(phase='route' if item['kind'] == 'incoming' else 'compose', status='awaiting')
            self.record(item, 'checked', '候选检查通过' if passed else '候选检查未通过 · 空名称没有抛出异常', item['check'])
            if not passed and item['kind'] == 'local':
                item.update(status='blocked', error='检查未通过，先修订当前候选。')
        elif phase == 'apply':
            self.apply_result(item)
        elif phase == 'push':
            result = self.publish_action('duo', 'remote.push', {'source': item['result']['oid']})
            item.update(pushed=result, phase='done', status='done')
            self.record(item, 'pushed', '已推送本地演示远端，并核对实际提交', result)
        else:
            raise Problem('INPUT_REQUIRED', '当前步骤需要选择或填写内容')

    def apply_result(self, item):
        plan = item['plan']
        require(self.target_tip(item) == item['expected_target'], 'TARGET_MOVED', '目标已变化。原计划保留，请重新准备材料。')
        source = item['source']
        if plan['target'] == 'workspace':
            require(item['decision'] == 'workspace' and item['note'].strip(), 'HANDOFF_REQUIRED', '先填写接手要求')
            repo = self.flow_repo('workspace')
            require(item['expected_target'] is None, 'BRANCH_EXISTS', '接手分支已存在，请调整分支名')
            self.git_at(repo, 'fetch', '--no-tags', str(self.flow_repo(item['source_role'])), source)
            self.git_at(repo, 'update-ref', 'refs/heads/'+plan['branch'], source, '0'*40)
            item['result'] = {'role': 'workspace', 'branch': plan['branch'], 'oid': source,
                              'meaning': '已创建工作分支；原候选及未完成事项保留'}
            item.update(phase='done', status='done')
            self.record(item, 'adopted', '已接手到 Workspace · 仍需完善', item['result'])
            return
        require(item['check'] and item['check']['state'] == 'passed', 'CHECK_REQUIRED', '正式接纳需要当前候选检查通过')
        require(item['formal_id'], 'DRAFT_REQUIRED', '先确认正式草稿')
        formal = next(f for f in self.s['formal_commits'] if f['id'] == item['formal_id'])
        require(formal['state'] == 'local', 'ALREADY_APPLIED', '该草稿已执行')
        target = item['expected_target']
        merged = self.git('merge-tree', '--write-tree', target, source, check=False)
        require(merged.returncode == 0, 'CONFLICT', '合并预演有冲突；正式目标未改变。', details=merged.stdout.decode())
        tree = merged.stdout.decode().splitlines()[0]
        result = self.git('commit-tree', tree, '-p', target, data=item['message'].encode()).stdout.decode().strip()
        self.git('update-ref', 'refs/demo/plans/'+item['id'], result)
        check = self.check(result)
        require(check['state'] == 'passed', 'COMBINED_CHECK_FAILED', '组合结果检查未通过，正式目标未改变', details=check)
        self.require_outbound(result, already_packaged=True)
        self.git('update-ref', 'refs/heads/master', result, target)
        formal.update(state='synced', synced=True, result=result)
        item['result'] = {'role': 'release', 'branch': 'master', 'oid': result, 'source': source, 'checks': check}
        item.update(phase='push' if plan['push'] else 'done', status='ready' if plan['push'] else 'done')
        self.record(item, 'formalized', '正式提交已写入 Release · '+result[:8], item['result'])
        if plan['push'] and plan['auto_apply']:
            self.schedule(item)

    def execute_action(self, action, item, args):
        if action == 'settings':
            section = args['section']
            require(section in ('commit_format', 'privacy', 'remote_url'), 'BAD_SECTION', '设置不存在')
            if section == 'commit_format':
                require(re.fullmatch(r'[A-Z0-9]{1,16}', str(args['value'].get('prefix', ''))), 'BAD_PREFIX', '前缀使用 1–16 位大写字母或数字')
            result = self.publish_action('duo', 'publish.set', args)
            for other in self.items:
                if other['status'] == 'running':
                    other.update(status='paused', error='配置已变化，已暂停后续步骤。')
                    other['version'] += 1
            return result
        if action == 'reset':
            old = self.s['session']
            rev = self.s['revision']
            # Preserve decisions, drafts and notes along with the previous Git fixtures.
            (self.root / old / 'flow-state.json').write_text(fixture.encode(self.s), encoding='utf-8')
            self.s = self.seed()
            self.s['revision'] = rev
            self.ensure_publish()
            self.ensure_workflow()
            self.due.clear()
            return {'retained': old}
        require(item, 'ITEM_REQUIRED', '先选择事项')
        if action == 'resume':
            require(item['status'] == 'paused', 'NOT_PAUSED', '当前未暂停')
            if item['phase'] in ('prepare', 'check', 'apply', 'push'):
                self.schedule(item)
            else:
                item.update(status='awaiting', error='')
        elif action == 'continue':
            require(item['phase'] in ('prepare', 'check', 'apply', 'push'), 'INPUT_REQUIRED', '先完成当前选择或填写')
            require(item['status'] != 'running', 'ALREADY_RUNNING', '当前步骤正在执行')
            self.schedule(item)
        elif action == 'pause':
            require(item['status'] != 'done', 'COMPLETE', '该事项已完成当前处理')
            item['status'] = 'paused'
            self.record(item, 'paused', '已暂停后续步骤；已完成的操作保留')
        elif action == 'plan':
            require(not item['result'], 'COMPLETE', '已有实际结果，不能修改过去的计划')
            key, value = args['key'], args['value']
            require(key in ('auto_prepare', 'auto_apply', 'target', 'branch', 'push'), 'BAD_PLAN', '未知选项')
            if key in ('auto_prepare', 'auto_apply', 'push'):
                require(type(value) is bool, 'BAD_VALUE', '请选择开或关')
            if key == 'target':
                require(value in ('workspace', 'release'), 'BAD_TARGET', '目标无效')
                require(item['kind'] == 'incoming' or value == 'release', 'BAD_TARGET', '本地整理的目标为 Release')
                item['plan']['branch'] = 'master' if value == 'release' else 'incoming/offline-17'
                item['decision'] = None
                item['phase'] = 'route' if item['evidence'] and item['check'] else 'prepare'
            if key == 'branch':
                require(item['plan']['target'] == 'workspace', 'FIXED_TARGET', '此示例正式目标固定为 master')
                self.branch_name(value)
            item['plan'][key] = value
            if item['status'] == 'running':
                item['status'] = 'paused'
            if key in ('target', 'branch'):
                item['expected_target'] = self.target_tip(item)
                item['status'] = 'awaiting' if item['phase'] == 'route' else 'ready'
            self.record(item, 'plan_changed', '已调整后续流程', {'key': key, 'value': value})
        elif action == 'route':
            require(item['phase'] == 'route', 'WRONG_STEP', '当前不在接纳方式选择步骤')
            choice = args['choice']
            require(choice in ('workspace', 'release', 'discard'), 'BAD_CHOICE', '选项不存在')
            if choice == 'discard':
                item.update(phase='done', status='done', decision=choice)
                self.record(item, 'discarded', '本次暂不采用 · 来源提交保留')
            else:
                if choice == 'release':
                    require(item['check']['state'] == 'passed', 'CHECK_REQUIRED', '当前检查失败；可接手继续加工，或接收修订后再检查')
                item['decision'] = choice
                item['plan']['target'] = choice
                item['plan']['branch'] = 'master' if choice == 'release' else 'incoming/offline-17'
                item['expected_target'] = self.target_tip(item)
                item.update(phase='compose' if choice == 'release' else 'handoff', status='awaiting')
        elif action == 'handoff':
            require(item['phase'] == 'handoff', 'WRONG_STEP', '当前无需交接说明')
            value = str(args.get('text', '')).strip()
            require(0 < len(value) <= 5000, 'TEXT_REQUIRED', '请填写接手要求')
            item.update(note=value, phase='apply', status='ready')
            self.record(item, 'handoff', '已记录接手要求', {'text': value})
            if item['plan']['auto_apply']:
                self.schedule(item)
        elif action == 'compose':
            require(item['phase'] == 'compose', 'WRONG_STEP', '当前不在整理正式草稿步骤')
            require(item['check']['state'] == 'passed', 'CHECK_REQUIRED', '先修订并重新检查')
            message = str(args.get('text', '')).strip()
            require(re.match(r'^\[[A-Z0-9]+-\d+\]\s+\w+', message), 'BAD_FORMAT', '首行使用 [PREFIX-N] type: subject 格式')
            formal = self.formal_record(item['source'], message)
            item.update(message=message, formal_id=formal['id'], phase='apply', status='ready')
            self.record(item, 'drafted', '正式草稿已保存 · 尚未写入 Release', {'formal': formal['id']})
            if item['plan']['auto_apply']:
                self.schedule(item)
        elif action == 'reprepare':
            require(not item['result'], 'COMPLETE', '该事项已有实际结果')
            if item['formal_id']:
                next(f for f in self.s['formal_commits'] if f['id'] == item['formal_id'])['state'] = 'dissolved'
            item.update(phase='prepare', status='ready', evidence=None, check=None, formal_id=None, decision=None, error='')
            self.record(item, 'reprepare', '重新准备当前候选；原检查与操作记录保留')
        elif action == 'revision':
            require(item['kind'] == 'incoming' and not item['result'], 'UNAVAILABLE', '此处仅提供接纳前的作者修订示例')
            files = self.files(item['source'])
            files['download.py'] = "def normalize_name(name):\n    cleaned = name.strip()\n    if not cleaned:\n        raise ValueError('package name is required')\n    return cleaned.replace(' ', '-')\n"
            head = self.make_commit(self.repo, files, [item['source']], 'fix: reject empty package names [A4]', 'Lin')
            self.git('update-ref', 'refs/heads/feature/offline', head, item['source'])
            self.git_at(self.flow_repo('trial'), 'fetch', '--no-tags', str(self.repo), head)
            self.git_at(self.flow_repo('trial'), 'update-ref', 'refs/heads/incoming/17', head, item['source'])
            item['history'].append({'oid': head, 'label': 'A4'})
            item.update(source=head, label='A4', phase='prepare', status='ready', check=None, evidence=None, decision=None, formal_id=None, error='')
            self.record(item, 'revision', '已接收作者修订 A4 · 需要重新检查', {'oid': head})
        elif action == 'note':
            text = str(args.get('text', '')).strip()
            require(0 < len(text) <= 5000, 'TEXT_REQUIRED', '请输入讨论内容')
            item['notes'].append({'author': 'Duo', 'text': text, 'time': now(), 'source': item['source']})
        else:
            raise Problem('UNKNOWN_ACTION', '动作不存在', 404)
        return {'item': item['id'], 'phase': item['phase'], 'status': item['status']}

    def command(self, payload):
        with self.lock:
            request_id = str(payload.get('request_id', ''))
            require(re.fullmatch(r'[\w-]{8,80}', request_id), 'REQUEST_ID', '请求身份无效')
            fingerprint = fixture.digest(fixture.encode({k:v for k,v in payload.items() if k != 'request_id'}).encode())
            receipt = self.db.execute('SELECT fingerprint, body FROM receipts WHERE id=?', (request_id,)).fetchone()
            if receipt:
                require(receipt[0] == fingerprint, 'REQUEST_REUSED', '请求身份不能用于另一项操作')
                return json.loads(receipt[1])
            action = payload.get('action')
            item = self.item(payload['item']) if payload.get('item') else None
            if item:
                require(payload.get('version') == item['version'], 'STALE_ITEM', '事项已变化，已刷新。请查看当前状态后再操作。')
            result = self.execute_action(action, item, payload.get('args') or {})
            self.changed(item)
            body = {'ok': True, 'result': result, 'revision': self.s['revision']}
            self.db.execute('INSERT INTO receipts VALUES(?,?,?,?,?)', (request_id, 'duo', fingerprint, fixture.encode(body), now()))
            self.db.commit()
            return body

    def tick(self):
        with self.lock:
            for item in self.items:
                if item['status'] != 'running' or time.monotonic() < self.due.get(item['id'], 0):
                    continue
                try:
                    self.step(item)
                except Exception as error:
                    item.update(status='blocked', error=str(error))
                    self.record(item, 'blocked', str(error), getattr(error, 'details', None))
                self.changed(item)


class Handler(fixture.Handler):
    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == '/api/state':
                return self.reply(200, self.demo.view())
            if path == '/api/evidence':
                return self.reply(200, self.demo.view())
            names = {'/':'index.html', '/index.html':'index.html', '/app.js':'app.js', '/style.css':'style.css'}
            if path == '/commit-layout.js':
                return self.reply(200, (PREVIOUS/'commit-layout.js').read_bytes(), 'text/javascript; charset=utf-8')
            require(path in names, 'NOT_FOUND', '页面不存在', 404)
            name = names[path]
            mime = 'text/javascript' if name.endswith('.js') else mimetypes.guess_type(name)[0]
            self.reply(200, (ROOT/name).read_bytes(), mime+'; charset=utf-8')
        except Problem as error:
            self.error(error)
        except Exception as error:
            self.reply(500, {'ok': False, 'error': {'message': str(error)}})

    def do_POST(self):
        try:
            require(self.path == '/api/command', 'NOT_FOUND', '接口不存在', 404)
            require(self.headers.get('X-Demo-Client') == 'terminal-flow', 'BAD_CLIENT', '需要同源客户端', 403)
            origin = self.headers.get('Origin')
            require(not origin or origin == 'http://'+self.headers.get('Host', ''), 'BAD_ORIGIN', '需要同源请求', 403)
            require(self.headers.get('Content-Type', '').startswith('application/json'), 'BAD_TYPE', '需要 JSON', 415)
            size = int(self.headers.get('Content-Length', '0'))
            require(0 < size <= 40000, 'BAD_SIZE', '请求大小无效', 413)
            self.reply(200, self.demo.command(json.loads(self.rfile.read(size))))
        except Problem as error:
            self.error(error)
        except (ValueError, KeyError, TypeError) as error:
            self.reply(422, {'ok': False, 'error': {'message': str(error)}})
        except Exception as error:
            self.reply(500, {'ok': False, 'error': {'message': str(error)}})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--data', type=Path, default=ROOT/'runtime')
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    flow = Flow(args.data)
    http = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    http.demo = flow
    stop = threading.Event()
    def worker():
        while not stop.wait(.15):
            flow.tick()
    threading.Thread(target=worker, daemon=True).start()
    print(f'http://127.0.0.1:{args.port}/', flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{args.port}/')
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        http.server_close()


if __name__ == '__main__':
    main()
