# Reference extract from Gitgo c8e7906e6ed5e23af29ebdbc51e4bfafe696926b
from __future__ import annotations
import json
import os
import sys
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from backend.core.config import ConfigManager
_DEFAULT_HEADER = '[{prefix}-{number}] {type_str}{scope_str}: {subject}'
_DEFAULT_BODY = 'Project: {project_name}\n\nSynced from {commit_count} workspace commit(s):\n{commit_list}\n\n---\n\n# 请编辑正式 commit message（以上为模板，删除此说明行）\n'
