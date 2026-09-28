"""Multi-signal, recall-first dependency graph.

The graph deliberately favours false positives over false negatives.  Every
edge carries evidence and confidence, so consumers can decide how much work to
do without pretending that static analysis is complete.

Edge direction is ``dependent -> dependency``.  For example, when
``api.py`` imports ``auth.py``, the stored edge is ``api.py -> auth.py`` and
``get_dependents("auth.py")`` returns ``api.py``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable


GRAPH_VERSION = 2
GRAPH_FILE = "dependency_graph.v2.json"
FEEDBACK_FILE = "dependency_feedback.json"
OBSERVATION_FILE = "dependency_observations.jsonl"

_IGNORED_PARTS = {
    ".git", ".gitgo", ".venv", "venv", "node_modules", "__pycache__",
    "build", "dist", "out", "target", ".pytest_cache", ".mypy_cache",
}
_SOURCE_EXTENSIONS = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".c", ".cc",
    ".cpp", ".cxx", ".h", ".hpp", ".rb", ".php", ".swift", ".scala",
    ".vue", ".svelte", ".html", ".css", ".scss", ".sql", ".sh", ".ps1",
    ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".xml",
}
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_SYMBOL_FANOUT = 24
_MAX_SHARED_KEY_FANOUT = 12


def dependency_graph_affected_by(paths: Iterable[str]) -> bool:
    """Return whether changed workspace paths can alter the graph model."""
    auxiliary = {
        f".gitgo/{FEEDBACK_FILE}".casefold(),
        f".gitgo/{OBSERVATION_FILE}".casefold(),
    }
    for value in paths:
        normalised = _normalise_rel(value).casefold()
        if normalised in auxiliary or Path(normalised).suffix in _SOURCE_EXTENSIONS:
            return True
    return False


@dataclass
class DependencyEvidence:
    signal: str
    confidence: float
    detail: str = ""
    count: int = 1

    def to_dict(self) -> dict:
        return {
            "signal": self.signal,
            "confidence": round(float(self.confidence), 4),
            "detail": self.detail,
            "count": self.count,
        }


@dataclass
class DependencyEdge:
    dependent: str
    dependency: str
    evidence: list[DependencyEvidence] = field(default_factory=list)
    dismissed: bool = False
    dismissal_reason: str = ""

    @property
    def confidence(self) -> float:
        # Independent weak signals reinforce one another without exceeding 1.
        miss_probability = 1.0
        for item in self.evidence:
            conf = max(0.0, min(1.0, float(item.confidence)))
            miss_probability *= (1.0 - conf)
        return round(1.0 - miss_probability, 4)

    def to_dict(self) -> dict:
        return {
            "dependent": self.dependent,
            "dependency": self.dependency,
            "confidence": self.confidence,
            "dismissed": self.dismissed,
            "dismissal_reason": self.dismissal_reason,
            "evidence": [item.to_dict() for item in self.evidence],
        }


class DependencyGraph:
    """In-memory graph with evidence-aware merge and query operations."""

    def __init__(self, workspace_path: Path):
        self.workspace_path = workspace_path.resolve()
        self.edges: dict[tuple[str, str], DependencyEdge] = {}
        self.file_fingerprints: dict[str, str] = {}
        self.aux_fingerprints: dict[str, str] = {}
        self.generated_at = datetime.now().isoformat()

    def add_edge(
        self,
        dependent: str,
        dependency: str,
        *,
        signal: str,
        confidence: float,
        detail: str = "",
        count: int = 1,
    ) -> None:
        dependent = _normalise_rel(dependent)
        dependency = _normalise_rel(dependency)
        if not dependent or not dependency or dependent == dependency:
            return
        key = (dependent, dependency)
        edge = self.edges.setdefault(key, DependencyEdge(dependent, dependency))
        for existing in edge.evidence:
            if existing.signal == signal and existing.detail == detail:
                existing.count += count
                existing.confidence = max(existing.confidence, confidence)
                return
        edge.evidence.append(DependencyEvidence(signal, confidence, detail, count))

    def get_dependents(
        self,
        file_path: str,
        *,
        min_confidence: float = 0.0,
        include_dismissed: bool = False,
    ) -> list[dict]:
        raw_target = str(file_path)
        if Path(raw_target).is_absolute():
            target = _normalise_workspace_file(self.workspace_path, raw_target)
        else:
            target = _normalise_rel(raw_target)
        targets = {target} if target else set()
        # Legacy callers sometimes pass a repository-relative path while the
        # graph workspace is a repository subdirectory (workspace=backend,
        # query=backend/core/history.py).  Preserve that API without storing
        # duplicate edges.  An unresolved basename intentionally fans out to
        # every matching file: recall is safer than silently choosing one.
        prefix = self.workspace_path.name.replace("\\", "/") + "/"
        if target.startswith(prefix):
            targets.add(target[len(prefix):])
        if not (targets & self.file_fingerprints.keys()):
            targets.update(
                rel for rel in self.file_fingerprints
                if rel == target or rel.endswith("/" + target)
                or Path(rel).name == Path(target).name
            )
        result = []
        for edge in self.edges.values():
            if edge.dependency not in targets:
                continue
            if edge.dismissed and not include_dismissed:
                continue
            if edge.confidence < min_confidence:
                continue
            result.append(edge.to_dict())
        return sorted(result, key=lambda item: (-item["confidence"], item["dependent"]))

    def to_dict(self) -> dict:
        return {
            "version": GRAPH_VERSION,
            "generated_at": self.generated_at,
            "workspace": str(self.workspace_path),
            "file_fingerprints": self.file_fingerprints,
            "aux_fingerprints": self.aux_fingerprints,
            "edges": [
                self.edges[key].to_dict()
                for key in sorted(self.edges)
            ],
        }

    @classmethod
    def from_dict(cls, workspace_path: Path, data: dict) -> "DependencyGraph":
        graph = cls(workspace_path)
        graph.generated_at = str(data.get("generated_at", ""))
        graph.file_fingerprints = dict(data.get("file_fingerprints", {}))
        graph.aux_fingerprints = dict(data.get("aux_fingerprints", {}))
        for raw in data.get("edges", []):
            edge = DependencyEdge(
                dependent=_normalise_rel(raw.get("dependent", "")),
                dependency=_normalise_rel(raw.get("dependency", "")),
                dismissed=bool(raw.get("dismissed", False)),
                dismissal_reason=str(raw.get("dismissal_reason", "")),
            )
            for item in raw.get("evidence", []):
                edge.evidence.append(DependencyEvidence(
                    signal=str(item.get("signal", "unknown")),
                    confidence=float(item.get("confidence", 0.0)),
                    detail=str(item.get("detail", "")),
                    count=int(item.get("count", 1)),
                ))
            if edge.dependent and edge.dependency and edge.dependent != edge.dependency:
                graph.edges[(edge.dependent, edge.dependency)] = edge
        return graph


def build_dependency_graph(workspace_path: Path) -> DependencyGraph:
    workspace = workspace_path.resolve()
    graph = DependencyGraph(workspace)
    files = _source_files(workspace)
    contents: dict[str, str] = {}

    for path in files:
        rel = _relative(workspace, path)
        graph.file_fingerprints[rel] = _fingerprint(path)
        try:
            contents[rel] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            contents[rel] = ""

    aliases = _build_path_aliases(contents)

    # High-confidence direct language imports/includes/requires.
    for rel, content in contents.items():
        for raw_ref, detail in _extract_import_references(rel, content):
            for target in _resolve_reference(rel, raw_ref, aliases):
                graph.add_edge(
                    rel, target, signal="language_reference", confidence=0.92,
                    detail=detail or raw_ref,
                )

    # Quoted paths, templates, configs and assets often express dependencies
    # that language import parsers cannot see.
    for rel, content in contents.items():
        for raw_ref in _extract_path_references(content):
            for target in _resolve_reference(rel, raw_ref, aliases):
                graph.add_edge(
                    rel, target, signal="path_reference", confidence=0.78,
                    detail=raw_ref,
                )

    _add_symbol_edges(graph, contents)
    _add_shared_key_edges(graph, contents)
    _add_test_edges(graph, contents)
    _add_git_cochange_edges(graph)
    _add_observation_edges(graph)
    _apply_feedback(graph)
    graph.aux_fingerprints = _aux_fingerprints(workspace)
    _save_graph(graph)
    return graph


def load_dependency_graph(
    workspace_path: Path,
    *,
    rebuild: bool = False,
    allow_stale: bool = False,
) -> DependencyGraph:
    workspace = workspace_path.resolve()
    path = workspace / ".gitgo" / GRAPH_FILE
    if not rebuild and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") == GRAPH_VERSION:
                graph = DependencyGraph.from_dict(workspace, data)
                # Read-side shortcuts such as a bounded code dossier must not
                # turn a five-file query into a synchronous whole-repository
                # rebuild.  They surface snapshot freshness separately and the
                # explicit rebuild tool remains the authority for refreshing.
                if allow_stale:
                    return graph
                if (
                    graph.file_fingerprints == _current_fingerprints(workspace)
                    and graph.aux_fingerprints == _aux_fingerprints(workspace)
                ):
                    return graph
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    return build_dependency_graph(workspace)


def query_dependents(
    workspace_path: Path,
    file_path: str,
    *,
    min_confidence: float = 0.0,
    include_dismissed: bool = False,
) -> list[dict]:
    return load_dependency_graph(workspace_path).get_dependents(
        file_path,
        min_confidence=min_confidence,
        include_dismissed=include_dismissed,
    )


def record_dependency_feedback(
    workspace_path: Path,
    *,
    dependent: str,
    dependency: str,
    confirmed: bool,
    reason: str = "",
    source: str = "reviewer",
) -> dict:
    """Persist a confirmed or dismissed edge with code-version fingerprints."""
    workspace = workspace_path.resolve()
    dependent = _normalise_rel(dependent)
    dependency = _normalise_rel(dependency)
    if not dependent or not dependency or dependent == dependency:
        raise ValueError("feedback requires two distinct workspace-relative files")
    dep_path = workspace / dependent
    target_path = workspace / dependency
    entry = {
        "dependent": dependent,
        "dependency": dependency,
        "confirmed": bool(confirmed),
        "reason": reason.strip(),
        "source": source.strip() or "reviewer",
        "recorded_at": datetime.now().isoformat(),
        "dependent_fingerprint": _fingerprint(dep_path) if dep_path.exists() else "",
        "dependency_fingerprint": _fingerprint(target_path) if target_path.exists() else "",
    }
    path = workspace / ".gitgo" / FEEDBACK_FILE
    data = {"version": 1, "entries": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    entries = [
        item for item in data.get("entries", [])
        if not (
            _normalise_rel(item.get("dependent", "")) == dependent
            and _normalise_rel(item.get("dependency", "")) == dependency
        )
    ]
    entries.append(entry)
    data = {"version": 1, "entries": entries}
    _atomic_json_write(path, data)
    build_dependency_graph(workspace)
    return entry


def record_tool_observation(
    workspace_path: Path,
    *,
    task_id: str,
    tool_name: str,
    files: Iterable[str],
    outcome: str = "observed",
) -> None:
    """Record co-access as a weak dependency signal for later graph rebuilds."""
    workspace = workspace_path.resolve()
    normalised = sorted({
        _normalise_workspace_file(workspace, item)
        for item in files if isinstance(item, str) and item.strip()
    } - {""})
    if not task_id or not normalised:
        return
    path = workspace / ".gitgo" / OBSERVATION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "task_id": task_id,
        "tool_name": tool_name,
        "files": normalised,
        "recorded_at": datetime.now().isoformat(),
        "outcome": outcome,
    }
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        return


def _source_files(workspace: Path) -> list[Path]:
    result = []
    try:
        candidates = workspace.rglob("*")
    except OSError:
        return result
    for path in candidates:
        try:
            if not path.is_file() or path.suffix.lower() not in _SOURCE_EXTENSIONS:
                continue
            if any(part.lower() in _IGNORED_PARTS for part in path.relative_to(workspace).parts):
                continue
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            result.append(path)
        except (OSError, ValueError):
            continue
    return sorted(result, key=lambda item: _relative(workspace, item))


def _current_fingerprints(workspace: Path) -> dict[str, str]:
    return {_relative(workspace, path): _fingerprint(path) for path in _source_files(workspace)}


def _fingerprint(path: Path) -> str:
    try:
        # Content identity is required for feedback validity.  Size/mtime-only
        # fingerprints can survive generated-file rewrites or timestamp
        # restoration and would incorrectly keep a dismissed edge suppressed.
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(128 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()[:20]
    except OSError:
        return ""


def _build_path_aliases(contents: dict[str, str]) -> dict[str, set[str]]:
    aliases: dict[str, set[str]] = {}
    for rel in contents:
        path = Path(rel)
        candidates = {
            rel,
            rel.lstrip("./"),
            str(path.with_suffix("")).replace("\\", "/"),
            path.name,
            path.stem,
            str(path.with_suffix("")).replace("\\", "/").replace("/", "."),
        }
        if path.name in {"index.js", "index.ts", "index.tsx", "__init__.py"}:
            candidates.add(str(path.parent).replace("\\", "/"))
            candidates.add(str(path.parent).replace("\\", "/").replace("/", "."))
        for alias in candidates:
            if alias and alias != ".":
                aliases.setdefault(alias.lower(), set()).add(rel)
    return aliases


def _extract_import_references(rel: str, content: str) -> list[tuple[str, str]]:
    suffix = Path(rel).suffix.lower()
    refs: list[tuple[str, str]] = []
    if suffix in {".py", ".pyi"}:
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    refs.extend((alias.name, f"import {alias.name}") for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    prefix = "." * node.level
                    module = node.module or ""
                    refs.append((prefix + module, f"from {prefix + module}"))
                    if not module:
                        refs.extend(
                            (prefix + alias.name, f"from {prefix} import {alias.name}")
                            for alias in node.names
                        )
        except SyntaxError:
            pass
    patterns = [
        r"\b(?:import|export)\s+(?:[^'\"]+?\s+from\s+)?['\"]([^'\"]+)['\"]",
        r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
        r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
        r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]",
        r"^\s*(?:use|mod)\s+([\w:]+)",
        r"^\s*require(?:_relative)?\s*['\"]([^'\"]+)['\"]",
        r"^\s*(?:using|import)\s+([\w.]+)\s*;",
        r"^\s*(?:source|\.)\s+['\"]?([^'\"\s]+)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, content, re.M):
            refs.append((match.group(1), match.group(0)[:160]))
    return refs


def _extract_path_references(content: str) -> set[str]:
    result = set()
    pattern = r"['\"]([^'\"\n]{1,180}\.(?:py|pyi|js|jsx|mjs|cjs|ts|tsx|go|rs|java|kt|cs|c|cc|cpp|h|hpp|rb|php|swift|scala|vue|svelte|html|css|scss|sql|json|toml|ya?ml|ini|cfg|xml))['\"]"
    for match in re.finditer(pattern, content, re.I):
        result.add(match.group(1).replace("\\", "/"))
    return result


def _resolve_reference(
    source_rel: str,
    raw_ref: str,
    aliases: dict[str, set[str]],
) -> set[str]:
    raw = raw_ref.strip().replace("\\", "/")
    if not raw or raw.startswith(("http://", "https://", "data:")):
        return set()
    source_dir = str(Path(source_rel).parent).replace("\\", "/")
    candidates = {raw, raw.lstrip("./")}
    if raw.startswith("."):
        candidates.add(os.path.normpath(f"{source_dir}/{raw}").replace("\\", "/"))
    dotted = raw.lstrip(".").replace("::", ".")
    candidates.add(dotted)
    candidates.add(dotted.replace(".", "/"))
    expanded = set(candidates)
    for item in list(candidates):
        for ext in _SOURCE_EXTENSIONS:
            expanded.add(item + ext)
        expanded.add(item.rstrip("/") + "/index.ts")
        expanded.add(item.rstrip("/") + "/index.js")
        expanded.add(item.rstrip("/") + "/__init__.py")
    result: set[str] = set()
    for candidate in expanded:
        result.update(aliases.get(candidate.lower(), set()))
    return result


def _add_symbol_edges(graph: DependencyGraph, contents: dict[str, str]) -> None:
    definitions: dict[str, set[str]] = {}
    definition_patterns = [
        r"\b(?:def|class|function|interface|type|struct|enum|trait|fn)\s+([A-Za-z_]\w{2,})",
        r"\b(?:const|let|var)\s+([A-Za-z_]\w{2,})\s*=",
    ]
    for rel, content in contents.items():
        for pattern in definition_patterns:
            for name in re.findall(pattern, content):
                definitions.setdefault(name, set()).add(rel)
    usable = {
        name: files for name, files in definitions.items()
        if len(files) <= _MAX_SYMBOL_FANOUT
    }
    for rel, content in contents.items():
        words = set(re.findall(r"\b[A-Za-z_]\w{2,}\b", content))
        for name in words & usable.keys():
            for target in usable[name]:
                if target != rel:
                    graph.add_edge(
                        rel, target, signal="symbol_reference", confidence=0.42,
                        detail=name,
                    )


def _add_shared_key_edges(graph: DependencyGraph, contents: dict[str, str]) -> None:
    owners: dict[str, set[str]] = {}
    for rel, content in contents.items():
        keys = set()
        for value in re.findall(r"['\"]([A-Za-z_][A-Za-z0-9_.:/-]{4,80})['\"]", content):
            if any(mark in value for mark in ("_", ".", ":", "/", "-")):
                keys.add(value)
            if len(keys) >= 250:
                break
        for key in keys:
            owners.setdefault(key, set()).add(rel)
    for key, files in owners.items():
        if not 1 < len(files) <= _MAX_SHARED_KEY_FANOUT:
            continue
        ordered = sorted(files)
        for source in ordered:
            for target in ordered:
                if source != target:
                    graph.add_edge(
                        source, target, signal="shared_contract_key", confidence=0.16,
                        detail=key,
                    )


def _add_test_edges(graph: DependencyGraph, contents: dict[str, str]) -> None:
    by_stem: dict[str, list[str]] = {}
    for rel in contents:
        stem = Path(rel).stem.lower()
        canonical = re.sub(r"^(test_|spec_)", "", stem)
        canonical = re.sub(r"(_test|_tests|_spec)$", "", canonical)
        by_stem.setdefault(canonical, []).append(rel)
    for rel in contents:
        name = Path(rel).stem.lower()
        if not ("test" in name or "spec" in name or "/tests/" in f"/{rel.lower()}/"):
            continue
        canonical = re.sub(r"^(test_|spec_)", "", name)
        canonical = re.sub(r"(_test|_tests|_spec)$", "", canonical)
        for target in by_stem.get(canonical, []):
            if target != rel:
                graph.add_edge(
                    rel, target, signal="test_subject", confidence=0.72,
                    detail=canonical,
                )


def _add_git_cochange_edges(graph: DependencyGraph) -> None:
    try:
        completed = subprocess.run(
            ["git", "log", "-120", "--name-only", "--pretty=format:@@"],
            cwd=str(graph.workspace_path), capture_output=True, text=True,
            timeout=12,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if completed.returncode != 0:
        return
    commits: list[list[str]] = []
    current: list[str] = []
    for line in completed.stdout.splitlines():
        value = line.strip()
        if value == "@@":
            if current:
                commits.append(current)
            current = []
        elif value:
            current.append(_normalise_rel(value))
    if current:
        commits.append(current)
    counts: dict[tuple[str, str], int] = {}
    known = set(graph.file_fingerprints)
    for files in commits:
        group = sorted(set(files) & known)
        if not 1 < len(group) <= 20:
            continue
        for source in group:
            for target in group:
                if source != target:
                    counts[(source, target)] = counts.get((source, target), 0) + 1
    for (source, target), count in counts.items():
        confidence = min(0.18 + 0.06 * count, 0.58)
        graph.add_edge(
            source, target, signal="git_cochange", confidence=confidence,
            detail=f"{count} recent commits", count=count,
        )


def _add_observation_edges(graph: DependencyGraph) -> None:
    path = graph.workspace_path / ".gitgo" / OBSERVATION_FILE
    if not path.exists():
        return
    by_task: dict[str, set[str]] = {}
    failed_tasks: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-5000:]
    except OSError:
        return
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        task_id = str(item.get("task_id", ""))
        if task_id:
            by_task.setdefault(task_id, set()).update(item.get("files", []))
            if item.get("outcome") == "failure":
                failed_tasks.add(task_id)
    counts: dict[tuple[str, str], int] = {}
    failure_counts: dict[tuple[str, str], int] = {}
    known = set(graph.file_fingerprints)
    for task_id, files in by_task.items():
        group = sorted({_normalise_rel(item) for item in files} & known)
        if not 1 < len(group) <= 30:
            continue
        for source in group:
            for target in group:
                if source != target:
                    counts[(source, target)] = counts.get((source, target), 0) + 1
                    if task_id in failed_tasks:
                        failure_counts[(source, target)] = failure_counts.get((source, target), 0) + 1
    for (source, target), count in counts.items():
        graph.add_edge(
            source, target, signal="tool_coaccess",
            confidence=min(0.12 + 0.07 * count, 0.62),
            detail=f"{count} tasks", count=count,
        )
        if failure_counts.get((source, target), 0):
            failures = failure_counts[(source, target)]
            graph.add_edge(
                source, target, signal="failure_coaccess",
                confidence=min(0.40 + 0.10 * failures, 0.80),
                detail=f"{failures} failed tasks", count=failures,
            )


def _apply_feedback(graph: DependencyGraph) -> None:
    path = graph.workspace_path / ".gitgo" / FEEDBACK_FILE
    if not path.exists():
        return
    try:
        entries = json.loads(path.read_text(encoding="utf-8")).get("entries", [])
    except (OSError, json.JSONDecodeError):
        return
    for item in entries:
        source = _normalise_rel(item.get("dependent", ""))
        target = _normalise_rel(item.get("dependency", ""))
        if not source or not target:
            continue
        # A dismissal is scoped to the exact code versions that were reviewed.
        if not item.get("confirmed", False):
            if item.get("dependent_fingerprint", "") != graph.file_fingerprints.get(source, ""):
                continue
            if item.get("dependency_fingerprint", "") != graph.file_fingerprints.get(target, ""):
                continue
            edge = graph.edges.get((source, target))
            if edge:
                edge.dismissed = True
                edge.dismissal_reason = str(item.get("reason", "reviewed false positive"))
            continue
        graph.add_edge(
            source, target, signal="confirmed_feedback", confidence=1.0,
            detail=str(item.get("reason", "confirmed by reviewer")),
        )


def _save_graph(graph: DependencyGraph) -> None:
    _atomic_json_write(graph.workspace_path / ".gitgo" / GRAPH_FILE, graph.to_dict())


def _atomic_json_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _aux_fingerprints(workspace: Path) -> dict[str, str]:
    result = {}
    for name in (FEEDBACK_FILE, OBSERVATION_FILE):
        path = workspace / ".gitgo" / name
        result[name] = _fingerprint(path) if path.exists() else ""
    return result


def _normalise_workspace_file(workspace: Path, value: str) -> str:
    try:
        path = Path(value)
        if not path.is_absolute():
            path = workspace / path
        resolved = path.resolve(strict=False)
        resolved.relative_to(workspace)
        return _relative(workspace, resolved)
    except (OSError, ValueError):
        return ""


def _normalise_rel(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _relative(workspace: Path, path: Path) -> str:
    return str(path.relative_to(workspace)).replace("\\", "/")
