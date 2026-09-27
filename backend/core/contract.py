"""Project Contract — 项目合约 + 漂移检测

sync 成功后自动维护合约，push 前检测漂移。
合约格式: .gitgo/contract.yaml（人可读可改）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class DecidedFeature:
    name: str
    location: str = ""
    signature: str = ""
    confirmed_count: int = 1
    introduced: str = ""
    last_modified: str = ""


@dataclass
class ProjectContract:
    project: str = ""
    updated: str = ""
    tech_stack: list[str] = field(default_factory=list)
    decided_features: list[DecidedFeature] = field(default_factory=list)
    architecture_constraints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "updated": self.updated,
            "tech_stack": self.tech_stack,
            "decided_features": [
                {
                    "name": f.name,
                    "location": f.location,
                    "signature": f.signature,
                    "confirmed_count": f.confirmed_count,
                    "introduced": f.introduced,
                    "last_modified": f.last_modified,
                }
                for f in self.decided_features
            ],
            "architecture_constraints": self.architecture_constraints,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ProjectContract:
        features = []
        for fd in d.get("decided_features", []):
            if isinstance(fd, dict):
                features.append(DecidedFeature(
                    name=fd.get("name", ""),
                    location=fd.get("location", ""),
                    signature=fd.get("signature", ""),
                    confirmed_count=fd.get("confirmed_count", 1),
                    introduced=fd.get("introduced", ""),
                    last_modified=fd.get("last_modified", ""),
                ))
        return cls(
            project=d.get("project", ""),
            updated=d.get("updated", ""),
            tech_stack=d.get("tech_stack", []),
            decided_features=features,
            architecture_constraints=d.get("architecture_constraints", []),
        )


class ContractManager:
    """管理项目合约的读写。"""

    CONTRACT_FILE = "contract.yaml"

    @staticmethod
    def path(workspace_path: Path) -> Path:
        return workspace_path / ".gitgo" / ContractManager.CONTRACT_FILE

    @staticmethod
    def load(workspace_path: Path) -> ProjectContract | None:
        p = ContractManager.path(workspace_path)
        if not p.exists():
            return None
        try:
            import yaml
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            return ProjectContract.from_dict(data)
        except Exception:
            return None

    @staticmethod
    def save(workspace_path: Path, contract: ProjectContract) -> Path:
        p = ContractManager.path(workspace_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        contract.updated = datetime.now().strftime("%Y-%m-%d")
        import yaml
        p.write_text(
            yaml.dump(contract.to_dict(), allow_unicode=True, default_flow_style=False,
                      sort_keys=False),
            encoding="utf-8",
        )
        return p

    @staticmethod
    def update_feature(workspace_path: Path, project_name: str,
                       feature_name: str, location: str = "",
                       signature: str = "") -> ProjectContract:
        """更新或新增 decided feature。"""
        contract = ContractManager.load(workspace_path) or ProjectContract(
            project=project_name,
        )
        contract.project = project_name

        for f in contract.decided_features:
            if f.name == feature_name:
                f.confirmed_count += 1
                f.last_modified = datetime.now().strftime("%Y-%m-%d")
                if location:
                    f.location = location
                if signature:
                    f.signature = signature
                ContractManager.save(workspace_path, contract)
                return contract

        # 新 feature
        today = datetime.now().strftime("%Y-%m-%d")
        contract.decided_features.append(DecidedFeature(
            name=feature_name,
            location=location,
            signature=signature,
            introduced=today,
            last_modified=today,
        ))
        ContractManager.save(workspace_path, contract)
        return contract


# ── 漂移检测 ──────────────────────────────────────────────

def detect_drift(
    workspace_path: Path,
    changed_files: list[str],
    contract: ProjectContract,
) -> list[dict]:
    """检测本轮变更与合约的偏差。返回告警列表。"""
    if contract is None:
        return []
    alerts = []

    # Rule 1: 功能删除检测
    for feat in contract.decided_features:
        if not feat.location:
            continue
        fpath = workspace_path / feat.location
        if feat.location in changed_files or not fpath.exists():
            if not fpath.exists():
                alerts.append({
                    "rule": "feature_deleted",
                    "level": "error",
                    "message": (
                        f"Feature '{feat.name}' file '{feat.location}' "
                        f"is missing. Confirmed {feat.confirmed_count}x. "
                        f"This may be LLM avoiding implementation."
                    ),
                    "feature": feat.name,
                    "location": feat.location,
                    "confirmed_count": feat.confirmed_count,
                })
            elif feat.signature:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
                if feat.signature not in content:
                    alerts.append({
                        "rule": "feature_signature_lost",
                        "level": "error",
                        "message": (
                            f"Signature '{feat.signature}' of feature "
                            f"'{feat.name}' not found in '{feat.location}'. "
                            f"The implementation may have been removed."
                        ),
                        "feature": feat.name,
                        "signature": feat.signature,
                    })

    # Rule 2: 技术栈漂移 — 检测新增未声明 import
    if contract.tech_stack:
        new_imports = _detect_new_imports(workspace_path, changed_files, contract)
        if new_imports:
            alerts.append({
                "rule": "tech_stack_drift",
                "level": "warning",
                "message": (
                    f"New imports detected: {', '.join(new_imports[:5])}. "
                    f"Not in declared tech_stack: {contract.tech_stack}. "
                    f"This may indicate LLM switching technology."
                ),
                "new_imports": new_imports,
            })

    # Rule 3: 架构约束违反
    for constraint in contract.architecture_constraints:
        if constraint not in _CONSTRAINT_CHECKS:
            alerts.append({
                "rule": "architecture_constraint_unverified",
                "level": "warning",
                "message": (
                    f"Architecture constraint has no registered deterministic checker: "
                    f"'{constraint}'. Review is required; no violation is inferred from words."
                ),
                "constraint": constraint,
                "machine_verifiable": False,
            })
            continue
        violations = _check_architecture_constraint(
            workspace_path, changed_files, constraint,
        )
        if violations:
            alerts.append({
                "rule": "architecture_violation",
                "level": "error",
                "message": (
                    f"Architecture constraint violated: '{constraint}'. "
                    f"Found in: {', '.join(violations[:3])}"
                ),
                "constraint": constraint,
                "violations": violations,
            })

    return alerts


_PY_IMPORT_RE = re.compile(r'^\s*(?:import|from)\s+(\w+)', re.M)
_BUILTIN_MODULES = {
    "os", "sys", "re", "json", "pathlib", "datetime", "shutil", "tempfile",
    "subprocess", "typing", "dataclasses", "collections", "itertools",
    "functools", "hashlib", "uuid", "math", "random", "time", "io", "csv",
    "argparse", "logging", "unittest", "abc", "enum", "textwrap", "copy",
    "traceback", "warnings", "contextlib", "inspect", "importlib",
    "__future__", "base64", "struct", "socket", "threading", "queue",
    "fnmatch", "glob", "getpass", "platform",
}


def _detect_new_imports(
    workspace_path: Path,
    changed_files: list[str],
    contract: ProjectContract,
) -> list[str]:
    """扫描变更文件中的新 import，与合约 tech_stack 对比。"""
    declared = set(contract.tech_stack)
    found = set()
    for rel_path in changed_files:
        if not rel_path.endswith(".py"):
            continue
        fpath = workspace_path / rel_path
        if not fpath.exists():
            continue
        try:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _PY_IMPORT_RE.finditer(content):
            mod = m.group(1)
            if mod.startswith("_"):
                continue
            if mod in _BUILTIN_MODULES:
                continue
            # 检查是否属于声明技术栈的一部分
            is_declared = False
            for ts in declared:
                if mod == ts or mod.startswith(ts + ".") or ts.startswith(mod):
                    is_declared = True
                    break
            if not is_declared:
                found.add(mod)
    return sorted(found)


# 架构约束 → 正则检测规则
_CONSTRAINT_CHECKS = {
    "不使用绝对定位": (re.compile(r'\b(?:move|resize|setGeometry)\s*\('), "*.py"),
    "不跳过 git hooks": (re.compile(r'--no-verify|--no-gpg-sign|commit\.gpgsign'), "*.py"),
    "不直接 mutation core": (re.compile(r'session\.(?:formal_commits|commits|entries)\s*[.\[=]'), "*.py"),
    "信号槽新式写法": (re.compile(r'\.exec_\('), "*.py"),
}


def _check_architecture_constraint(
    workspace_path: Path,
    changed_files: list[str],
    constraint: str,
) -> list[str]:
    """检查架构约束是否被违反。"""
    violations = []

    # 先查内置规则
    if constraint in _CONSTRAINT_CHECKS:
        pattern, glob_pat = _CONSTRAINT_CHECKS[constraint]
        for rel_path in changed_files:
            if not _match_glob(rel_path, glob_pat):
                continue
            fpath = workspace_path / rel_path
            if not fpath.exists():
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if pattern.search(content):
                violations.append(rel_path)
    else:
        # Unknown prose constraints are not executable specifications.  The
        # caller emits an explicit unverified warning instead of manufacturing
        # a violation from a keyword hit.
        return []

    return violations


def _match_glob(path: str, pattern: str) -> bool:
    from fnmatch import fnmatch
    return fnmatch(path.replace("\\", "/"), pattern)


def check_feature_signatures(
    workspace_path: Path,
    changed_files: list[str],
    contract: ProjectContract,
) -> list[dict]:
    """检查 decided_features 的签名是否仍存在于变更文件中。

    当变更文件列表包含某个 decided_feature 的 location 时，
    验证其 signature 仍然可以被找到。签名消失 → 依赖断裂告警。
    """
    alerts = []
    for feat in contract.decided_features:
        if not feat.location or not feat.signature:
            continue
        if feat.location not in changed_files:
            continue
        fpath = workspace_path / feat.location
        if not fpath.exists():
            continue
        try:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if feat.signature not in content:
            alerts.append({
                "rule": "feature_signature_lost",
                "level": "error",
                "message": (
                    f"Dependency break: '{feat.signature}' of feature "
                    f"'{feat.name}' not found in '{feat.location}' "
                    f"after modification."
                ),
                "feature": feat.name,
                "signature": feat.signature,
                "location": feat.location,
            })
    return alerts


# ── Dependency Graph ──────────────────────────────────────

_DEP_GRAPH_FILE = "dep_graph.json"


def build_dep_graph(workspace_path: Path) -> dict[str, list[str]]:
    """Build the legacy reverse-import view from the multi-signal graph.

    The returned shape remains ``{module_stem: [dependent files...]}`` for old
    callers.  New code should use :mod:`backend.core.dependency_graph`, whose
    edges preserve full paths, evidence, confidence, feedback and languages.
    """
    from backend.core.dependency_graph import build_dependency_graph

    rich = build_dependency_graph(workspace_path)
    graph: dict[str, list[str]] = {}
    for edge in rich.edges.values():
        if edge.dismissed:
            continue
        dependency = Path(edge.dependency)
        aliases = {dependency.stem, edge.dependency}
        for alias in aliases:
            graph.setdefault(alias, []).append(edge.dependent)
    for key in graph:
        graph[key] = sorted(set(graph[key]))
    dep_path = workspace_path / ".gitgo" / _DEP_GRAPH_FILE
    dep_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    dep_path.write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")
    return graph


def load_dep_graph(workspace_path: Path) -> dict[str, list[str]]:
    """Load a fresh legacy view; rich-graph fingerprints own invalidation."""
    from backend.core.dependency_graph import load_dependency_graph

    rich = load_dependency_graph(workspace_path)
    graph: dict[str, list[str]] = {}
    for edge in rich.edges.values():
        if edge.dismissed:
            continue
        dependency = Path(edge.dependency)
        for alias in {dependency.stem, edge.dependency}:
            graph.setdefault(alias, []).append(edge.dependent)
    return {key: sorted(set(values)) for key, values in graph.items()}


def get_dependents(workspace_path: Path, file_path: str) -> list[str]:
    """查询一个文件被哪些其他文件 import。

    file_path: 相对于 workspace 的路径，如 'lexi/classifier.py'
    """
    from backend.core.dependency_graph import query_dependents

    edges = query_dependents(workspace_path, file_path)
    return sorted({edge["dependent"] for edge in edges})


# ── v0.36: AST 函数级依赖图（Level 2: 反向图差分）─────────

import ast as _ast
import json as _json

_FUNC_GRAPH_FILE = "func_graph.json"


def _parse_ast_symbols(file_path: Path) -> dict:
    """解析单个 Python 文件的 AST，提取函数/类及其调用关系。

    Returns:
        {"defines": ["func1", "ClassA.method"], "calls": ["other_func", "ClassB.x"]}
    """
    if not file_path.exists() or not file_path.suffix == ".py":
        return {"defines": [], "calls": []}

    try:
        tree = _ast.parse(file_path.read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, OSError):
        return {"defines": [], "calls": []}

    defines = []
    calls = []
    current_class = None

    for node in _ast.walk(tree):
        # 类定义
        if isinstance(node, _ast.ClassDef):
            current_class = node.name
            defines.append(node.name)
        # 函数定义
        elif isinstance(node, _ast.FunctionDef):
            name = f"{current_class}.{node.name}" if current_class else node.name
            defines.append(name)
        # 调用点
        elif isinstance(node, _ast.Call):
            if isinstance(node.func, _ast.Name):
                calls.append(node.func.id)
            elif isinstance(node.func, _ast.Attribute):
                if isinstance(node.func.value, _ast.Name):
                    calls.append(f"{node.func.value.id}.{node.func.attr}")

    return {"defines": defines, "calls": list(set(calls))}


def build_function_graph(workspace_path: Path) -> dict[str, dict]:
    """构建函数级调用图：file:func → 谁调了它。

    返回:
        {"auth.py": {"authenticate": ["login.py:handle_login", "api.py:verify"]}}

    Level 1 fallback: 如果 AST 解析失败，降级到 import 级图。
    """
    graph: dict[str, dict[str, list[str]]] = {}
    parsed: dict[str, dict] = {}

    # Pass 1 collects every definition before any call is resolved.  The old
    # one-pass implementation silently missed callees that happened to be
    # visited later by rglob(), making the graph filesystem-order dependent.
    for py_file in sorted(workspace_path.rglob("*.py")):
        if ".git" in py_file.parts or "__pycache__" in py_file.parts:
            continue

        rel = str(py_file.relative_to(workspace_path)).replace("\\", "/")
        symbols = _parse_ast_symbols(py_file)

        if not symbols["defines"] and not symbols["calls"]:
            continue

        parsed[rel] = symbols
        graph[rel] = {"defines": symbols["defines"], "called_by": {}}

    definition_index: dict[str, list[str]] = {}
    for rel, symbols in parsed.items():
        for defined in symbols["defines"]:
            definition_index.setdefault(defined, []).append(rel)
            definition_index.setdefault(defined.split(".")[-1], []).append(rel)

    # Pass 2 resolves simple and qualified calls against the complete index.
    # Ambiguous symbols intentionally create multiple candidates: this graph is
    # used for impact discovery, where a false positive is cheaper than a miss.
    for caller_rel, symbols in parsed.items():
        for called in symbols["calls"]:
            candidates = set(definition_index.get(called, []))
            candidates.update(definition_index.get(called.split(".")[-1], []))
            for callee_rel in candidates:
                if callee_rel == caller_rel:
                    continue
                callee_data = graph[callee_rel]
                matched = [
                    item for item in callee_data.get("defines", [])
                    if item == called or item.split(".")[-1] == called.split(".")[-1]
                ]
                for defined in matched:
                    refs = callee_data.setdefault("called_by", {}).setdefault(defined, [])
                    refs.append(f"{caller_rel}:{called}")

    for data in graph.values():
        for symbol, callers in data.get("called_by", {}).items():
            data["called_by"][symbol] = sorted(set(callers))

    # 持久化
    cache_path = workspace_path / ".gitgo" / _FUNC_GRAPH_FILE
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(_json.dumps(graph, indent=2, ensure_ascii=False),
                          encoding="utf-8")
    return graph


def load_function_graph(workspace_path: Path) -> dict[str, dict]:
    """加载缓存的函数级调用图。不存在则构建。

    Source fingerprint invalidation is owned by dependency_graph.v2; rebuild
    here when its generated graph is newer than the legacy function cache.
    """
    from backend.core.dependency_graph import load_dependency_graph

    # This refreshes the source fingerprint cache before comparing mtimes.
    load_dependency_graph(workspace_path)
    cache_path = workspace_path / ".gitgo" / _FUNC_GRAPH_FILE
    if cache_path.exists():
        try:
            rich_path = workspace_path / ".gitgo" / "dependency_graph.v2.json"
            if not rich_path.exists() or cache_path.stat().st_mtime_ns >= rich_path.stat().st_mtime_ns:
                return _json.loads(cache_path.read_text(encoding="utf-8"))
        except (_json.JSONDecodeError, OSError):
            pass
    return build_function_graph(workspace_path)


def get_callers(workspace_path: Path, file_path: str,
                func_name: str = "") -> list[str]:
    """反向图差分：查询哪些文件/函数依赖了 file_path 中的 func_name。

    func_name 为空时 → 返回 import 级别的所有依赖者（Level 1 fallback）。
    func_name 非空时 → 返回精确到函数的调用者。

    Returns:
        ["login.py:handle_login", "api.py:verify_token"]
    """
    rel = file_path.replace("\\", "/")
    try:
        graph = load_function_graph(workspace_path)
    except Exception:
        return get_dependents(workspace_path, file_path)

    file_data = graph.get(rel, {})
    if not func_name:
        # Level 1 fallback: import 级
        return get_dependents(workspace_path, file_path)

    # Level 2: 函数级精确查询
    called_by = file_data.get("called_by", {})
    return called_by.get(func_name, [])


def get_changed_symbols(file_path: Path, old_content: str = "",
                        new_content: str = "") -> list[str]:
    """对比文件两个版本的 AST，返回变更的函数/类名。

    用于 workspace_dirty 时精确判断"X 的哪个函数变了"。
    """
    if not file_path.suffix == ".py":
        return []

    old_symbols = set()
    new_symbols = set()

    if old_content:
        try:
            old = _ast.parse(old_content)
            old_symbols = {n.name for n in _ast.walk(old)
                          if isinstance(n, (_ast.FunctionDef, _ast.ClassDef))}
        except SyntaxError:
            pass

    if new_content:
        try:
            new = _ast.parse(new_content)
            new_symbols = {n.name for n in _ast.walk(new)
                          if isinstance(n, (_ast.FunctionDef, _ast.ClassDef))}
        except SyntaxError:
            pass

    changed = []
    # 新增或修改
    changed.extend(new_symbols - old_symbols)
    # 删除
    changed.extend(old_symbols - new_symbols)
    return list(changed)
