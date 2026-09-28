"""Deterministic code facts and task sharding for large Agent work.

This module deliberately produces no semantic audit conclusion.  It replaces
repetitive file enumeration, outline extraction, dependency lookup and token
arithmetic that an LLM should not spend reasoning steps performing.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path


_SOURCE_EXTENSIONS = {
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".cs", ".c", ".cc", ".cpp",
    ".h", ".hpp", ".rb", ".php", ".swift", ".scala", ".vue",
    ".svelte", ".html", ".css", ".scss", ".sql", ".sh", ".ps1",
    ".json", ".toml", ".yaml", ".yml", ".md",
}
_IGNORED_PARTS = {
    ".git", ".gitgo", ".claude", ".codex", ".venv", "venv", "node_modules", "__pycache__",
    "build", "dist", "out", "target", ".pytest_cache", ".mypy_cache",
}
_GENERIC_SYMBOL = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:class|interface|enum|type|function|def|fn|struct|trait)\s+([A-Za-z_$][\w$]*)"
)
_JS_IMPORT = re.compile(
    r"(?:from\s+|require\s*\(\s*|import\s*\(\s*)['\"]([^'\"]+)['\"]"
)


def _resolve_targets(workspace: Path, targets: list[str], max_files: int) -> list[Path]:
    candidates: list[Path] = []
    raw_targets = targets or ["."]
    for raw in raw_targets:
        item = Path(str(raw))
        resolved = item.resolve(strict=False) if item.is_absolute() else (
            workspace / item
        ).resolve(strict=False)
        try:
            resolved.relative_to(workspace)
        except ValueError as exc:
            raise PermissionError(f"dossier target escapes workspace: {raw}") from exc
        if resolved.is_file():
            candidates.append(resolved)
            continue
        if not resolved.exists():
            continue
        for path in resolved.rglob("*"):
            if len(candidates) >= max_files:
                break
            if not path.is_file() or path.suffix.lower() not in _SOURCE_EXTENSIONS:
                continue
            if any(part in _IGNORED_PARTS for part in path.relative_to(workspace).parts):
                continue
            candidates.append(path)
    unique = {path.relative_to(workspace).as_posix(): path for path in candidates}
    return [unique[name] for name in sorted(unique)[:max_files]]


def _python_facts(content: str) -> tuple[list[dict], list[str]]:
    symbols: list[dict] = []
    imports: list[str] = []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return symbols, imports
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append({
                "name": node.name,
                "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                "line": int(getattr(node, "lineno", 0)),
                "end_line": int(getattr(node, "end_lineno", getattr(node, "lineno", 0))),
            })
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * int(node.level or 0)
            imports.append(prefix + str(node.module or ""))
    return sorted(symbols, key=lambda item: (item["line"], item["name"])), sorted(set(imports))


def _generic_facts(content: str, suffix: str) -> tuple[list[dict], list[str]]:
    symbols = []
    for number, line in enumerate(content.splitlines(), 1):
        match = _GENERIC_SYMBOL.match(line)
        if match:
            symbols.append({"name": match.group(1), "kind": "symbol", "line": number})
    imports = sorted(set(_JS_IMPORT.findall(content))) if suffix in {
        ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    } else []
    return symbols, imports


def build_code_dossier(
    workspace_path: str | Path,
    target_files: list[str] | None = None,
    *,
    max_files: int = 240,
    max_symbols_per_file: int = 120,
    max_source_chars_per_file: int = 100_000,
) -> dict:
    workspace = Path(workspace_path).resolve()
    max_files = max(1, min(int(max_files), 1000))
    paths = _resolve_targets(workspace, list(target_files or []), max_files)
    graph = None
    try:
        from backend.core.dependency_graph import load_dependency_graph
        graph = load_dependency_graph(workspace, allow_stale=True)
    except (OSError, ValueError):
        graph = None
    files = []
    total_chars = 0
    matching_graph_fingerprints = 0
    for path in paths:
        rel = path.relative_to(workspace).as_posix()
        content = path.read_text(encoding="utf-8", errors="replace")
        total_chars += len(content)
        suffix = path.suffix.lower()
        symbols, imports = (
            _python_facts(content) if suffix in {".py", ".pyi"}
            else _generic_facts(content, suffix)
        )
        dependents = graph.get_dependents(rel) if graph is not None else []
        sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if graph is not None and graph.file_fingerprints.get(rel) == sha256:
            matching_graph_fingerprints += 1
        files.append({
            "path": rel,
            "language": suffix.lstrip(".") or "text",
            "bytes": path.stat().st_size,
            "lines": content.count("\n") + 1,
            "estimated_tokens": max(1, len(content) // 4),
            "sha256": sha256,
            # Bounded immutable source handoff. Line prefixes make exact audit
            # evidence deterministic and avoid repeated read_file calls.
            "numbered_source": "\n".join(
                f"{number:06d}: {line}"
                for number, line in enumerate(
                    content[:max_source_chars_per_file].splitlines(), 1
                )
            ),
            "source_truncated": len(content) > max_source_chars_per_file,
            "symbols": symbols[:max_symbols_per_file],
            "symbols_truncated": len(symbols) > max_symbols_per_file,
            "imports": imports[:100],
            "dependents": [{
                "path": edge.get("dependent", ""),
                "confidence": edge.get("confidence", 0),
                "signals": [item.get("signal", "") for item in edge.get("evidence", [])],
            } for edge in dependents[:40]],
            "is_test": bool(re.search(r"(^|/)(tests?|__tests__)(/|$)|(?:^|[._-])test(?:[._-]|$)", rel, re.I)),
        })
    estimated_tokens = max(1, total_chars // 4)
    if estimated_tokens <= 8_000 and len(files) <= 10:
        size_class = "small"
    elif estimated_tokens <= 32_000 and len(files) <= 40:
        size_class = "medium"
    else:
        size_class = "large"
    digest = hashlib.sha256(json.dumps(
        [(item["path"], item["sha256"]) for item in files],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "schema_version": 1,
        "workspace": workspace.name,
        "digest": digest,
        "targets": list(target_files or ["."]),
        "file_count": len(files),
        "estimated_tokens": estimated_tokens,
        "size_class": size_class,
        "truncated": len(paths) >= max_files,
        "dependency_graph": {
            "mode": "snapshot_allow_stale",
            "generated_at": getattr(graph, "generated_at", "") if graph else "",
            "target_fingerprints_matching": matching_graph_fingerprints,
            "target_fingerprints_total": len(files),
            "fresh_for_all_targets": bool(files) and matching_graph_fingerprints == len(files),
            "rebuild_required_for_authoritative_freshness": bool(files) and (
                matching_graph_fingerprints != len(files)
            ),
        },
        "files": files,
    }


def build_shard_dossier(dossier: dict, shard: dict) -> dict:
    """Project a global manifest into one immutable, model-ready handoff."""
    targets = set(str(item) for item in shard.get("target_files", []))
    files = [
        item for item in dossier.get("files", [])
        if str(item.get("path", "")) in targets
    ]
    return {
        "schema_version": dossier.get("schema_version", 1),
        "authority": "deterministic_host_snapshot",
        "semantic_conclusion": False,
        "workspace": dossier.get("workspace", ""),
        "dossier_digest": dossier.get("digest", ""),
        "shard_id": shard.get("shard_id", ""),
        "component": shard.get("component", ""),
        "target_files": sorted(targets),
        "estimated_tokens": shard.get("estimated_tokens", 0),
        "dependency_graph": dossier.get("dependency_graph", {}),
        "source_complete": (
            len(files) == len(targets)
            and not any(item.get("source_truncated", False) for item in files)
        ),
        "files": files,
    }


def suggest_task_shards(
    dossier: dict,
    *,
    goal: str,
    max_tokens_per_shard: int = 12_000,
    max_files_per_shard: int = 24,
    max_shards: int = 8,
) -> dict:
    """Create stable file shards; it does not invent semantic responsibilities."""
    max_tokens_per_shard = max(2_000, min(int(max_tokens_per_shard), 64_000))
    max_files_per_shard = max(1, min(int(max_files_per_shard), 100))
    max_shards = max(1, min(int(max_shards), 16))
    grouped: dict[str, list[dict]] = {}
    for item in dossier.get("files", []):
        parts = Path(str(item.get("path", ""))).parts
        component = parts[0] if len(parts) > 1 else "root"
        grouped.setdefault(component, []).append(item)
    shards: list[dict] = []
    for component in sorted(grouped):
        current: list[dict] = []
        token_count = 0
        for item in sorted(grouped[component], key=lambda row: row["path"]):
            item_tokens = int(item.get("estimated_tokens", 0) or 0)
            if current and (
                len(current) >= max_files_per_shard
                or token_count + item_tokens > max_tokens_per_shard
            ):
                shards.append({"component": component, "files": current, "tokens": token_count})
                current, token_count = [], 0
            current.append(item)
            token_count += item_tokens
        if current:
            shards.append({"component": component, "files": current, "tokens": token_count})
    overflow = len(shards) > max_shards
    if overflow:
        retained = shards[:max_shards - 1]
        tail = shards[max_shards - 1:]
        retained.append({
            "component": "overflow",
            "files": [item for shard in tail for item in shard["files"]],
            "tokens": sum(shard["tokens"] for shard in tail),
        })
        shards = retained
    result = []
    for index, shard in enumerate(shards, 1):
        paths = [item["path"] for item in shard["files"]]
        result.append({
            "shard_id": f"shard-{index:02d}",
            "component": shard["component"],
            "task_description": (
                f"{goal.strip()}\n\nInspect only this deterministic shard: "
                + ", ".join(paths)
            ),
            "target_files": paths,
            "estimated_tokens": shard["tokens"],
            "oversized": (
                shard["tokens"] > max_tokens_per_shard
                or len(paths) > max_files_per_shard
            ),
        })
    return {
        "dossier_digest": dossier.get("digest", ""),
        "goal": goal.strip(),
        "shards": result,
        "shard_count": len(result),
        "recommended_parallelism": min(4, max(1, len(result))),
        "overflow_merged": overflow,
        "requires_semantic_partition": any(item["oversized"] for item in result),
    }
