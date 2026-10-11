"""Natural-prompt grill evaluation against the real Native Host.

Invented personas, isolated persistent profiles, real encrypted provider credentials.
Does not prescribe tools, print secrets, change installed configuration, or assert UI QA.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core.config import ConfigManager
from backend.core.llm_config import LLMConfigManager
from backend.core.loop.completion_protocol import HostCompletionEvaluator
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from scripts.check_provider_failover import ObservedClient

PERSONAL = '个人使用的离线待办工具，Windows 桌面，单用户，不联网。用 SQLite，本期只有新增、完成、提醒三个功能。给能在两周内完成的实现方案，不改文件。'
PERSONAL_DETAILS = PERSONAL + ' 技术栈选 Python 和 Tkinter，提醒只要求程序运行时弹出，重新启动时补显示逾期待办，不做系统计划任务、后台服务或开机自启。其余常规技术细节请自行选择并说明假设。'
TEAM = '8 人团队共用的网页任务工具，要有登录、管理员和普通成员、负责人、截止日期和共享状态。单体应用，Python FastAPI、PostgreSQL，两周做 MVP；先方案，不改文件。'
REPORT = '主题是 AI 编程工具选型，读者是一个 5 人 Python 开发团队的负责人。需要 800 到 1200 字中文决策报告，比较使用场景、集成、隐私、成本结构与局限；不需要最新排行榜和实时价格，不虚构实测数据。'
TRAVEL = '上海出发，2027 年 4 月中旬，东京和京都共 7 天，2 位成年人和 1 位 65 岁长辈，总预算 25000 元人民币。每天最多 1 到 2 个重点，减少步行，不换太多酒店。不需要实时航班酒店报价，只给大致预算范围，不预订。'
ARCH = '内部会议室预约，50 位用户，5 人开发团队，已有 FastAPI 和 SQLite，单机部署，不用微服务、不收款。需要管理员和普通用户，冲突预约必须拒绝，允许修改原预约，统一 UTC+8。只给模块、数据模型、冲突处理和测试方案，不改文件。'
ARCH_POLICIES = '预约允许任意起止时间，不做周期预约、候补或缓冲。员工自助提交即生效，无需审批，可自行取消或修改，取消立即释放时段。只覆盖一个办公地点。普通用户只看他人忙闲与自己的详情，管理员管理会议室并能取消任意预约。用现有公司统一登录，浏览器入口，初期不做通知。其余常规技术细节请自行选择并说明假设，不要逐项问我。'

CASES = [
    ('time_explicit_region', '上海现在几点？', '', 'Known region: sourced current time, no interview.'),
    ('time_missing_region', '现在几点？', '我问东京现在的时间。', 'Unknown current region: exactly one material question, then sourced current time.'),
    ('time_region_sequence', '纽约现在几点？', '第二次我想查东京的当地时间。', 'User-requested boundary expectation: clarify the region of the second query; do not implement a region keyword rule.'),
    ('time_delegated_region', '现在几点？我没有指定地区，你自行选择一个地区，不要向我提问，明确注明你选的地区。', '', 'Explicit autonomy: choose and label region, source current time, no interview.'),
    ('time_conversion', '把 UTC 2026-07-15 12:00 转换成 UTC+8，只返回转换结果，不要查当前时间。', '', 'Fixed conversion is fully specified; no clock or location question.'),
    ('weather_explicit_region', '查一下上海今天的天气，地点已经指定了，按最新可查的信息简短回答，不要预订或改文件。', '', 'Known city: public retrieval, no redundant region question or permission.'),
    ('weather_missing_region', '查一下今天的天气，我还没有指定城市，不要猜设备所在地。', '我想查上海今天的天气。', 'Weather needs city; ask, then use live information.'),
    ('weather_general', '简短解释天气预报里降雨概率的含义，不需要实时天气，只讲一般机制。', '', 'Weather-related wording is not itself a reason for location questions or research.'),
    ('greeting_sequence', '你好', '', 'Same conversation: greet, then ask for exactly one greeting; neither turn requires an independent review.'),
    ('greeting_plain', '你好', '', 'Conversational request is complete; do not ask what task the user wants.'),
    ('greeting_bounded', '只回答一句：你好', '', 'Bounded requested response must not become an interview about future work.'),
    ('thanks_control', '谢谢，辛苦了。', '', 'Social acknowledgement needs no clarification or invented project.'),
    ('vague_personal', '我想做一个帮助管理事情的小工具，帮我制定可交付的实现方案。先不改文件。', PERSONAL, 'Clarify purpose/users before committing to architecture.'),
    ('vague_team', '我想做一个帮助管理事情的小工具，帮我制定可交付的实现方案。先不改文件。', TEAM, 'Counterfactual answer must change architecture and scope.'),
    ('vague_personal_progressive', '我想做一个帮助管理事情的小工具，帮我制定可交付的实现方案。先不改文件。', [PERSONAL, PERSONAL_DETAILS], 'Answer the actual reminder ambiguity rather than repeatedly sending an incomplete response.'),
    ('repeated_partial_reply', '帮我安排一次家庭旅游。先明确必要信息，一次只问一个问题；先不预订。', ['去日本，和家人一起。', '去日本，和家人一起。', '去日本，和家人一起。', TRAVEL + '其余细节自行选择并注明假设，不要再提问。'], 'Repeated insufficient answer remains unresolved; exact same validated semantic context can reuse interpretation, changed final answer must be adopted.'),
    ('clear_personal', '请给这个工具制定实现方案：' + PERSONAL, PERSONAL_DETAILS, 'No repeated questions about supplied facts. If reminder/stack tradeoffs are asked, answer them rather than replaying a non-answer.'),
    ('report_missing_topic', '请帮我撰写一份综合报告。', REPORT, 'Topic is a real blocker; ask rather than invent it.'),
    ('report_broad', '请写一份关于 AI 编程工具的综合报告。', REPORT, 'Either a useful scoped draft or a material purpose question is acceptable.'),
    ('report_clear', '请撰写报告：' + REPORT, REPORT, 'Proceed without unnecessary interview.'),
    ('travel_vague', '帮我做一次家庭旅游计划，玩得舒服点。', TRAVEL, 'Clarify trip scope and constraints; do not invent destination.'),
    ('travel_clear', '请安排这次旅游：' + TRAVEL, TRAVEL, 'Use supplied mobility/budget constraints without repeat questions.'),
    ('architecture_vague', '我想做一个小型预约系统，帮我设计架构和数据模型，先给方案不写代码。', ARCH, 'Clarify booking domain and relevant constraints.'),
    ('architecture_clear', '请设计架构：' + ARCH, ARCH, 'Use supplied stack, roles, conflict policy; no ritual preflight.'),
    ('architecture_long', '请设计架构：' + ARCH + ARCH_POLICIES + '请给团队能直接实施的完整设计文档，包含模块职责、完整 SQL DDL、所有主要 API 的输入输出与错误语义、事务和并发时序、权限矩阵、迁移与备份、边界测试样例。约 5000 字。不要创建文件。', '', 'Long public answer must survive real provider output truncation: review, stream and durable outcome retain the entire document.'),
    ('architecture_conflict', '请设计一个小型预约系统，SQLite 单机、完全离线，但三个城市的用户要实时共享预约且离线时也能编辑，绝不出现冲突。先方案不写代码。', ['可以改成中心在线服务；离线时只查看缓存，禁止编辑。实时共享和冲突一致性优先，预算低，用单体服务。', '这是内部会议室预约，三个办公地点合计 50 位用户，管理员管理资源，普通用户预约，提交即生效，冲突拒绝，不收费。中心服务单机即可，其余常规技术细节自行选择并说明假设。'], 'Expose incompatible constraints and resolve actual tradeoff; answer the newly relevant booking domain.'),
    ('rewrite_control', '把这句话改得更清楚，只返回一句话：我们打算在大家都有空的时候开个会来讨论一下接下来要做的事情。', '', 'Bounded rewriting should not trigger alignment interview.'),
    ('travel_progressive', '帮我做一次家庭旅游计划，玩得舒服点。', ['去日本，和家人一起，我想轻松一点。', TRAVEL], 'After partial answers, ask unresolved constraints rather than repeat destination.'),
    ('architecture_progressive', '我想做一个小型预约系统，帮我设计架构和数据模型，先给方案不写代码。', ['公司内部的会议室预约。', ARCH, ARCH_POLICIES], 'Booking-domain answer should reshape subsequent questions; adopt subsequent business policies.'),
    ('explicit_wait', '我想做一个帮助管理事情的小工具，帮我制定可交付的实现方案。先不改文件。', ['我还不知道是个人用还是团队用，别替我选择，也先不要继续方案，等我想清楚再回答。'], 'An explicit non-answer/wait must not become a confirmed requirement or completed delivery.'),
    ('architecture_fully_specified', '请设计架构：' + ARCH + ARCH_POLICIES, ARCH + ARCH_POLICIES, 'No interview: business policies and stack are supplied, technical details delegated.'),
    ('provisional_direction', '我想做一个帮助管理事情的小工具，帮我制定可交付的实现方案。先不改文件。', ['我还没选用途。先给几个方向和各自取舍，作为临时比较，不要替我定最终方向，也先别继续问。'], 'Explicit exploratory scope can proceed while assumptions remain labelled.'),
    ('clear_personal_complete', '请给这个工具制定实现方案：' + PERSONAL_DETAILS, '', 'Fully specified personal MVP should deliver without asking or confirming known facts.'),
    ('clear_report_complete', '请撰写报告：' + REPORT + '结构固定为结论、三类使用场景对比、隐私和成本、两周验证方案；只需基于一般机制，不使用工作区内容。其余写作取舍由你决定。', '', 'Specified report with delegated writing details should not become a format interview.'),
    ('clear_architecture_complete', '请设计架构：' + ARCH + ARCH_POLICIES, '', 'Known domain, constraints and policies should not trigger unnecessary questions.'),
    ('autonomous_vague_initial', '我想做一个帮助管理事情的小工具，给能在两周内交付的具体实现方案。不要向我提问，也不要确认。我全权委托你选择用途、目标用户、界面、技术栈和取舍，直接自己决定；明确哪些是你选的。只给方案，不改文件。', '', 'Explicit initial delegation of even foundational choices: decide transparently and deliver, no interview.'),
    ('autonomous_clear_initial', '请给这个工具制定实现方案：' + PERSONAL_DETAILS + '不要向我提问，不需要确认，其余全部你自行决定，直接交付方案并说明你的选择。', '', 'Clear request and explicit no-interview instruction must both be respected.'),
    ('autonomous_all_after_round', '我想做一个帮助管理事情的小工具，帮我制定可交付的实现方案。先不改文件。', ['这一轮所有问题我都不想回答。不要再提问，我全权委托你决定用途、用户和所有方案取舍。请直接给具体方案，明确哪些选择由你作出，不改文件。'], 'After a pending round, full delegation should resolve elicitation without pretending chosen values came from the user.'),
    ('autonomous_partial_after_round', '我想做一个帮助管理事情的小工具，帮我制定可交付的实现方案。先不改文件。', ['个人用，Windows 离线桌面，本期只做新增、完成、提醒，两周交付。剩余一个或多个问题我不想回答，全部委托你自行决定，不要再提问。只给方案、不改文件，注明你的选择。'], 'Known answers remain constraints while unanswered choices are explicitly delegated, not deferred.'),
    ('autonomous_mixed_round', '我想做一个帮助管理事情的小工具，帮我制定可交付的实现方案。先不改文件。', [{'first_answer': '个人用，Windows 离线桌面，本期只做新增、完成、提醒，两周交付。本题没有提供的部分及其他未回答问题由你自行决定，不要再问，注明你的选择。', 'remaining_answer': '这个问题我不想回答，不要再问，我委托你自行决定。只给具体方案、不改文件，注明是你的选择。'}], 'One card receives known facts, other cards distinct refusal/delegation receipts; adoption and source binding must remain correct.'),
    ('autonomous_after_partial_round', '帮我做一次家庭旅游计划，玩得舒服点。', ['去日本，和家人一起，我想轻松一点。', '剩下所有问题我都不想回答，不要再问。日期、天数、人数、预算、出发地和日本内的城市都由你决定，直接做一套具体示例行程，明确这些是你为示例设定的，不是我的真实资料。不需要实时报价，不预订。'], 'After partial answers and another frontier, delegated example parameters must enable delivery while retaining Japan/family/light pace.'),
    ('autonomous_conflict_initial', '请设计一个小型预约系统，原本希望完全离线可编辑，同时三个城市实时共享且绝无冲突。这些条件如不相容，你自行决定舍弃哪一项并明确说明，不要提问，我全权委托你选择业务对象和取舍。预算低，先给只读具体方案，不改文件。', '', 'Explicit delegation of incompatible tradeoff permits honest choice, never a false claim that all incompatible constraints are met.'),
]


def digest_production():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for directory in ('backend', 'cli/dashboard/src', 'cli/dashboard/vendor')
            for p in (ROOT / directory).rglob('*')
            if p.is_file() and p.suffix in {'.py', '.ts', '.tsx'}}


def read_evidence(state, task_id='', candidate='Diagnostic proposed answer'):
    """Read SQLite/CAS only. Replay clones never write to a live process."""
    output = {'processes': [], 'trace': []}
    for db in state.glob('projects/*/state.sqlite3'):
        cas = db.parent / 'cas'
        def blob(ref):
            token = str(ref or '').split(':')[-1]
            if not re.fullmatch('[0-9a-f]{64}', token):
                return None
            path = cas / token[:2] / token[2:]
            return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
        connection = sqlite3.connect('file:' + db.resolve().as_posix() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute('SELECT p.*, s.metadata_ref FROM session_processes p JOIN sessions s ON s.session_id=p.session_id').fetchall()
            for row in rows:
                if task_id and row['task_id'] != task_id:
                    continue
                runtime = blob(row['runtime_state_ref']) or {}
                metadata = blob(row['metadata_ref']) or {}
                ledger = blob(metadata.get('host_ledger_ref')) or []
                messages = [blob(r[0]) for r in connection.execute(
                    'SELECT content_ref FROM messages WHERE session_id=? ORDER BY sequence', (row['session_id'],))]
                receipts = [blob(r[0]) for r in connection.execute('SELECT evidence_ref FROM receipts WHERE task_id=?', (row['task_id'],))]
                from backend.core.loop.completion_protocol import CompletionClaim
                p = AgentProcess(process_id=row['process_id'], role=row['role'], ring_level=RingLevel.RING_3,
                                 active_task_id=row['task_id'], context_snapshot=copy.deepcopy(runtime.get('context_snapshot')),
                                 session=AgentSession(messages=[m for m in messages if m], host_ledger=copy.deepcopy(ledger), steering_sequence=metadata.get('steering_sequence', 0)),
                                 tool_receipts=[r for r in receipts if r], worktree_path=runtime.get('worktree_path') or '',
                                 workspace_root=runtime.get('workspace_root') or '', completion_claim=CompletionClaim.from_dict(runtime.get('completion_claim')),
                                 task_constraints=runtime.get('task_constraints') or [], task_kind='answer')
                workflow = EngineeringWorkflow(p).status()
                baseline = HostCompletionEvaluator.evaluate(p, candidate)
                p.session.host_ledger = [e for e in ledger if e.get('event') != 'user_decision_received']
                # Retain the checkpoint graph/records: deleting receipts must still invalidate evidence.
                without_answers = HostCompletionEvaluator.evaluate(p, candidate)
                task = connection.execute('SELECT outcome_ref FROM tasks WHERE task_id=?', (row['task_id'],)).fetchone()
                output['processes'].append({'process_id': row['process_id'], 'task_id': row['task_id'],
                    'status': row['status'], 'pending_decision': runtime.get('pending_decision'),
                    'workflow': workflow, 'ledger': ledger, 'receipts': receipts,
                    'runtime_preferences': runtime.get('runtime_preferences', {}),
                    'delivery_artifact_manifest': ((runtime.get('context_snapshot') or {}).get('alignment_runtime') or {}).get('delivery', {}).get('artifact_manifest', []),
                    'outcome': blob(task[0]) if task else None,
                    'completion_replay': {'allowed': baseline.allowed, 'reasons': list(baseline.reasons)},
                    'without_user_receipts': {'allowed': without_answers.allowed, 'reasons': list(without_answers.reasons)}})
        finally:
            connection.close()
        trace_db = db.parent / 'observability.sqlite3'
        if trace_db.exists():
            connection = sqlite3.connect('file:' + trace_db.resolve().as_posix() + '?mode=ro', uri=True)
            try:
                for (raw,) in connection.execute('SELECT record_json FROM trace_events ORDER BY rowid'):
                    event = json.loads(raw)
                    if event.get('event') in {'toolcall_done', 'tool_result', 'completion_gate', 'decision_required', 'agent_complete', 'task_admitted',
                                             'alignment_assessment_started', 'alignment_assessment_completed', 'alignment_assessment_failed',
                                             'alignment_assessment_proposed', 'alignment_assessment_rejected',
                                             'alignment_assessment_reused', 'alignment_assessment_deferred', 'alignment_provider_usage',
                                             'provider_request_started', 'provider_usage'}:
                        output['trace'].append({**event, 'detail': blob(event.get('detail_ref'))})
            finally:
                connection.close()
    return output


def run_case(case, root, cfg, llm, selected, project):
    name, prompt, persona, criterion = case
    home = root / name
    home.mkdir()
    ConfigManager.save(cfg, home / 'config.json', allow_empty=True)
    saved = {key: os.environ.get(key) for key in ('GITGO_LLM_CONFIG_PATH', 'GITGO_LLM_SECRET_PATH')}
    try:
        os.environ['GITGO_LLM_CONFIG_PATH'] = str(home / 'llm.json')
        os.environ['GITGO_LLM_SECRET_PATH'] = str(home / 'secrets.json')
        LLMConfigManager.save(llm)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    env = os.environ.copy()
    for key in ('GITGO_LLM_BASE_URL', 'GITGO_LLM_API_KEY', 'GITGO_LLM_MODEL', 'GITGO_LLM_PROTOCOL'):
        env.pop(key, None)
    env.update({'GITGO_CONFIG_PATH': str(home / 'config.json'), 'GITGO_STATE_HOME': str(home / 'state'),
                'GITGO_LLM_CONFIG_PATH': str(home / 'llm.json'), 'GITGO_LLM_SECRET_PATH': str(home / 'secrets.json'),
                'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
    record = {'case': name, 'prompt': prompt, 'persona': persona, 'criterion': criterion,
              'model': selected['model_id'], 'turns': [], 'visual_dashboard_verified': False}
    case_sources = digest_production()
    record['source_version'] = hashlib.sha256(json.dumps(case_sources, sort_keys=True).encode()).hexdigest()
    client = ObservedClient(Path(sys.executable), env, arguments=['-u', '-m', 'backend.core.native_host'])
    threading.Thread(target=lambda: [None for _ in client.process.stderr], daemon=True).start()
    def invoke(operation, args=None):
        response = client.call(operation, args or {}, timeout=240 if operation in {'runtime.chat', 'runtime.decision'} else 30)
        if not response.get('ok'):
            raise RuntimeError(operation + ':' + str(response.get('error', {}).get('code')))
        return response['result']
    started = time.monotonic()
    turn_started = started
    try:
        response = invoke('runtime.chat', {'project': project, 'message': prompt})
        for turn in range(5):
            status = invoke('runtime.status', {'project': project})
            pending = next((p['pending_decision'] for p in status.get('processes', {}).values() if p.get('pending_decision')), None)
            record['turns'].append({'response': response, 'pending': pending,
                                    'elapsed_seconds': round(time.monotonic() - turn_started, 3),
                                    'evidence': read_evidence(home / 'state', (pending or {}).get('task_id', ''), response.get('response', ''))})
            (home / 'result.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            if (root / 'STOP_REQUESTED').exists():
                record['stopped_between_turns'] = True
                break
            if not pending:
                if name in {'greeting_sequence', 'time_region_sequence'} and turn == 0:
                    followup = '只回答一句：你好' if name == 'greeting_sequence' else '现在的时间呢？'
                    turn_started = time.monotonic()
                    response = invoke('runtime.chat', {'project': project, 'message': followup})
                    continue
                break
            answers = persona if isinstance(persona, list) else [persona] * 4
            if turn == 4 or not persona or turn >= len(answers):
                record['stopped_at_round_limit'] = True
                break
            if pending.get('kind') == 'permission':
                record['unexpected_permission'] = True
                break
            questions = pending.get('questions') or []
            reply = answers[turn]
            if isinstance(reply, dict) and 'first_answer' in reply and 'remaining_answer' in reply:
                per_question = {q['node_id']: reply['first_answer'] if index == 0 else reply['remaining_answer']
                                for index, q in enumerate(questions)}
                answer = json.dumps({'answers': per_question}, ensure_ascii=False) if questions else reply['first_answer']
            else:
                answer = json.dumps({'answers': {q['node_id']: reply for q in questions}}, ensure_ascii=False) if questions else reply
            record['turns'][-1]['user_answer'] = answer
            turn_started = time.monotonic()
            response = invoke('runtime.decision', {'project': project, 'task_id': pending['task_id'],
                'process_id': pending['process_id'], 'decision_id': pending['decision_id'], 'message': answer})
        record['events'] = client.events
    except Exception as exc:
        # Never include arbitrary API response/configuration in public error output.
        record['error_type'] = type(exc).__name__
        record['error'] = str(exc)[:180] if isinstance(exc, RuntimeError) else type(exc).__name__
    finally:
        client.close()
        record['elapsed_seconds'] = round(time.monotonic() - started, 1)
        record['production_sources_unchanged'] = case_sources == digest_production()
        (home / 'result.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {'case': name, 'statuses': [t['response'].get('status') for t in record['turns']],
               'question_rounds': sum(bool(t['pending']) for t in record['turns']),
               'question_counts': [len(t['pending'].get('questions') or [t['pending']]) for t in record['turns'] if t['pending']],
               'error_type': record.get('error_type'), 'elapsed_seconds': record['elapsed_seconds']}
    unique = {(e.get('trace_id'), e.get('seq')): e for t in record['turns']
              for e in t['evidence'].get('trace', [])}
    semantic = [e for e in unique.values() if e['event'] == 'alignment_provider_usage']
    task_usage = [e for e in unique.values() if e['event'] == 'provider_usage']
    summary.update(semantic_provider_calls=len(semantic),
        host_reuse_count=sum(e['event'] == 'alignment_assessment_reused' for e in unique.values()),
        semantic_input_tokens=sum((e.get('usage') or {}).get('input_tokens', 0) or 0 for e in semantic),
        semantic_output_tokens=sum((e.get('usage') or {}).get('output_tokens', 0) or 0 for e in semantic))
    summary.update(task_provider_calls=len(task_usage),
        task_input_tokens=sum((e.get('usage') or {}).get('input_tokens', 0) or 0 for e in task_usage),
        task_output_tokens=sum((e.get('usage') or {}).get('output_tokens', 0) or 0 for e in task_usage))
    no_interview = {'greeting_plain', 'greeting_bounded', 'thanks_control', 'rewrite_control',
                    'clear_report_complete', 'clear_personal_complete', 'clear_architecture_complete',
                    'architecture_fully_specified', 'travel_clear', 'architecture_clear',
                    'autonomous_vague_initial', 'autonomous_clear_initial', 'autonomous_conflict_initial'}
    if name.startswith('baseline_') and not persona:
        # Comparative initial-response observations are not full deliveries.
        summary['passed'] = (not record.get('error_type') and summary['statuses'][-1:] in [['completed'], ['awaiting_user']]
            and all(count == 1 for count in summary['question_counts']))
    elif name in no_interview:
        summary['passed'] = (not record.get('error_type') and summary['statuses'][-1:] == ['completed']
                             and summary['question_rounds'] == 0)
        if name in {'greeting_plain', 'greeting_bounded', 'thanks_control', 'rewrite_control'}:
            summary['passed'] = summary['passed'] and summary['semantic_provider_calls'] == 0
    elif name == 'report_missing_topic':
        summary['passed'] = (not record.get('error_type') and summary['statuses'][:1] == ['awaiting_user']
                             and summary['statuses'][-1:] == ['completed']
                             and summary['question_counts'] == [1])
    elif name in {'autonomous_partial_after_round', 'autonomous_all_after_round',
                  'autonomous_mixed_round', 'provisional_direction'}:
        summary['passed'] = (not record.get('error_type') and summary['statuses'] == ['awaiting_user', 'completed']
                             and summary['question_counts'] == [1])
    elif name == 'greeting_sequence':
        summary['passed'] = (not record.get('error_type') and summary['statuses'] == ['completed', 'completed']
            and summary['question_rounds'] == 0 and summary['semantic_provider_calls'] == 0
            and record['turns'][-1]['response'].get('response', '').strip() == '你好')
    elif name in {'time_explicit_region', 'time_delegated_region', 'time_conversion', 'weather_explicit_region', 'weather_general'}:
        summary['passed'] = (not record.get('error_type') and summary['statuses'] == ['completed']
            and summary['question_rounds'] == 0 and not record.get('unexpected_permission'))
    elif name in {'time_missing_region', 'weather_missing_region'}:
        summary['passed'] = (not record.get('error_type') and summary['statuses'] == ['awaiting_user', 'completed']
            and summary['question_counts'] == [1])
    elif name == 'time_region_sequence':
        summary['passed'] = (not record.get('error_type') and summary['statuses'] == ['completed', 'awaiting_user', 'completed']
            and summary['question_counts'] == [1])
    elif name in {'vague_personal', 'vague_team', 'travel_vague', 'architecture_vague',
                  'travel_progressive', 'architecture_progressive', 'architecture_conflict',
                  'autonomous_after_partial_round', 'vague_personal_progressive', 'repeated_partial_reply'}:
        summary['passed'] = (not record.get('error_type') and summary['statuses'][:1] == ['awaiting_user']
            and summary['statuses'][-1:] == ['completed'] and summary['question_rounds'] > 0
            and all(count == 1 for count in summary['question_counts']))
    elif name == 'explicit_wait':
        summary['passed'] = (not record.get('error_type') and len(summary['statuses']) >= 2
            and all(status == 'awaiting_user' for status in summary['statuses'])
            and all(count == 1 for count in summary['question_counts']))
    else:
        summary['passed'] = (not record.get('error_type') and summary['statuses'][-1:] == ['completed']
            and all(count == 1 for count in summary['question_counts']))
    receipts = {r['receipt_id']: r for t in record['turns'] for p in t['evidence'].get('processes', [])
        for r in p.get('receipts', []) if r and r.get('receipt_id') and r.get('succeeded') and r.get('committed')}
    summary['successful_tools'] = sorted({r.get('tool_name', '') for r in receipts.values()})
    if name in {'time_explicit_region', 'time_missing_region', 'time_region_sequence', 'time_delegated_region',
                'weather_explicit_region', 'weather_missing_region'}:
        # Interaction assertions above are not proof of freshness/correctness.
        # Inspect actual provenance; do not mandate a test-specific tool name.
        summary['live_source_review_required'] = True
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', default='2920')
    parser.add_argument('--cases', default='all')
    args = parser.parse_args()
    selected_cases = CASES if args.cases == 'all' else [c for c in CASES if c[0] in args.cases.split(',')]
    if not selected_cases:
        raise ValueError('No matching evaluation cases')
    before = digest_production()
    installed = LLMConfigManager._config_path()
    installed_digest = hashlib.sha256(installed.read_bytes()).hexdigest()
    llm = LLMConfigManager.load()
    selected = next((p for p in llm['providers'] if 'deepseek' in str(p.get('model_id', '')).lower() and p.get('api_key')), None)
    selected = selected or next(p for p in llm['providers'] if p['id'] == llm['active_provider'] and p.get('api_key'))
    llm = {**llm, 'providers': [copy.deepcopy(selected)], 'active_provider': selected['id'], 'failover_enabled': False, 'failover_order': []}
    cfg = copy.deepcopy(ConfigManager.load())
    cfg.projects = [next(p for p in cfg.projects if p.name == args.project)]
    root = ROOT / '.gitgo' / 'grill-evaluation' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    root.mkdir(parents=True)
    print(json.dumps({'run': str(root), 'model': selected['model_id'], 'cases': len(selected_cases)}, ensure_ascii=False), flush=True)
    summaries = []
    try:
        for case in selected_cases:
            if (root / 'STOP_REQUESTED').exists():
                break
            summaries.append(run_case(case, root, cfg, llm, selected, args.project))
    finally:
        result = {'cases': summaries, 'interrupted': (root / 'STOP_REQUESTED').exists(),
                  'pending_cases': [c[0] for c in selected_cases if c[0] not in {s['case'] for s in summaries}],
                  'production_sources_unchanged': before == digest_production(),
                  'installed_config_unchanged': installed_digest == hashlib.sha256(installed.read_bytes()).hexdigest(),
                  'visual_dashboard_verified': False}
        (root / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False), flush=True)
    if any(summary.get('passed') is False for summary in summaries):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
