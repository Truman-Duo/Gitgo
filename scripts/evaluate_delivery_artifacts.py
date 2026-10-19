"""Real Native Host delivery checks in an owned, isolated workspace."""

import sys as _development_sys
from pathlib import Path as _DevelopmentPath
_development_sys.path.insert(0, str(_DevelopmentPath(__file__).resolve().parents[1]))
from scripts.development_paths import development_root, test_state_home
from pathlib import Path
import copy
import hashlib
import json
from datetime import datetime
import uuid
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.evaluate_alignment_behavior import run_case, digest_production
from backend.core.config import ConfigManager
from backend.core.llm_config import LLMConfigManager


def main():
    root = development_root(ROOT) / 'delivery-evaluation' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    workspace = root / 'workspace'
    workspace.mkdir(parents=True)
    source = digest_production()
    installed = LLMConfigManager._config_path()
    installed_digest = hashlib.sha256(installed.read_bytes()).hexdigest()
    llm = LLMConfigManager.load()
    selected = next(p for p in llm['providers'] if p['id'] == llm['active_provider'] and p.get('api_key'))
    llm = {**llm, 'providers': [copy.deepcopy(selected)], 'failover_enabled': False, 'failover_order': []}
    cfg = copy.deepcopy(ConfigManager.load())
    cfg.projects = [copy.deepcopy(next(p for p in cfg.projects if p.name == '2920'))]
    cfg.projects[0].workspace_path = str(workspace)
    cfg.projects[0].backup_path = str(root / 'release')
    cfg.projects[0].trial = None
    cfg.projects[0].workspace.last_known_head = ''
    print(json.dumps({'run': str(root), 'model': selected['model_id']}, ensure_ascii=False), flush=True)
    cases = [
        ('file_after_clarification', '我想做一个小工具，但还没决定给谁用，你先帮我明确需求',
         '个人离线 Markdown 阅读器，先给完整方案，其余自行决定，不要再问。把完整方案保存为 plan.md，聊天只给一句摘要和文件路径，不重复文档全文。',
         'Actual card reply adopted; real file is the requested delivery, not a filename promise.'),
        ('inline_delivery', '给一个个人离线 Markdown 阅读器的简短方案，只在聊天正文回答，不创建或修改文件。形态与技术选型自行决定，不提问。', '',
         'Explicit inline delivery remains inline, without invented artifact evidence.'),
    ]
    summaries = []
    for case in cases:
        summary = run_case(case, root, cfg, llm, selected, '2920')
        record = json.loads((root / case[0] / 'result.json').read_text(encoding='utf-8'))
        processes = record['turns'][-1]['evidence']['processes']
        manifests = [p['delivery_artifact_manifest'] for p in processes if p.get('delivery_artifact_manifest')]
        replays = [p['completion_replay'] for p in processes if p.get('outcome') and p['outcome'].get('status') == 'completed']
        summary['artifact_used'] = bool(manifests)
        summary['completion_replay_allowed'] = bool(replays) and all(p['allowed'] for p in replays)
        assessments = [e for e in record['turns'][-1]['evidence']['trace'] if e['event'] == 'alignment_assessment_completed']
        summary['delivery_repairs'] = sum(e.get('stage') == 'delivery' and e.get('status') == 'repair' for e in assessments)
        summary['passed'] = bool(summary['passed'] and summary['completion_replay_allowed']
            and (bool(manifests) if case[0] == 'file_after_clarification' else not manifests))
        summaries.append(summary)
    result = {'cases': summaries, 'source_version': hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest(),
        'production_sources_unchanged': source == digest_production(),
        'installed_config_unchanged': installed_digest == hashlib.sha256(installed.read_bytes()).hexdigest(),
        'visual_dashboard_verified': False}
    (root / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if not all(s['passed'] for s in summaries):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
