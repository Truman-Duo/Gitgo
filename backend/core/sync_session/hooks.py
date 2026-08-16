"""决策钩子类型别名。

GUI/CUI 覆盖这些钩子以介入流程决策。真 import（类型别名在模块加载时求值，
不能依赖 ``from __future__ import annotations`` 的惰性求值）。
"""

from __future__ import annotations

from typing import Callable, Optional

from backend.core.config import ProjectConfig
from backend.core.operations import CommitInfo, FileEntry
from backend.models import IncomingChange


FileSelectionHook = Callable[[list[FileEntry]], list[FileEntry]]
CommitSelectionHook = Callable[[list[CommitInfo]], set[int]]
CommitMessageEditHook = Callable[[str, ProjectConfig], str | None]
SecurityWarningHook = Callable[[list[dict]], bool]
TriageHook = Callable[[list[IncomingChange], ProjectConfig], Optional[tuple[int, str]]]
