"""Host-only maintenance observations, never execution gates or LLM interviews."""
from __future__ import annotations
import ast
import hashlib
import json
import os
from pathlib import Path
import threading
from datetime import datetime, timezone

EXCLUDED = {".git", ".gitgo", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", "vendor", ".pytest_cache", "coverage"}
SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".c", ".cpp", ".h"}
BASELINE = ".gitgo-architecture.json"


def python_metrics(text, relative):
    """Static evidence only: no reflection, dynamic imports or semantic verdict."""
    tree = ast.parse(text)
    functions, complexity, imports = {}, {}, set()
    module = relative.removesuffix(".py").replace("/", ".")
    package = module if module.endswith(".__init__") else module.rpartition(".")[0]
    package = package.removesuffix(".__init__")

    def decisions(node):
        score = 0
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)):
                score += 1
            elif isinstance(child, ast.BoolOp):
                score += len(child.values) - 1
            elif isinstance(child, ast.comprehension):
                score += 1 + len(child.ifs)
            elif isinstance(child, ast.match_case):
                score += 1 + int(child.guard is not None)
            score += decisions(child)
        return score

    def visit(node, parents=()):
        for child in ast.iter_child_nodes(node):
            scopes = parents
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                scopes = (*parents, child.name)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = ".".join(scopes)
                functions[name] = child.end_lineno - child.lineno + 1
                # Include each top-level statement itself in the branch count.
                wrapper = ast.Module(body=child.body, type_ignores=[])
                complexity[name] = 1 + decisions(wrapper)
            visit(child, scopes)
    visit(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts = package.split(".") if package else []
            if node.level:
                parts = parts[:max(0, len(parts) - node.level + 1)]
                target = ".".join([*parts, *([node.module] if node.module else [])])
            else:
                target = node.module or ""
            if target:
                imports.add(target)
                imports.update(target + "." + alias.name for alias in node.names if alias.name != "*")
    return functions, complexity, sorted(imports)


def source_metrics(root):
    root = Path(root).resolve()
    metrics, issues, total = {}, [], 0
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(n for n in names if n not in EXCLUDED and not (Path(directory) / n).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix not in SUFFIXES or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if len(metrics) >= 5000:
                issues.append("source file limit (5000) reached")
                return metrics, issues
            try:
                size = path.stat().st_size
                if size > 2 * 1024 * 1024:
                    issues.append(relative + ": file exceeds scan size limit")
                    continue
                total += size
                if total > 64 * 1024 * 1024:
                    issues.append("source byte limit (64 MiB) reached")
                    return metrics, issues
                with path.open("rb") as handle:
                    data = handle.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    issues.append(relative + ": file grew beyond scan limit")
                    continue
                text = data.decode("utf-8-sig")
                functions, complexity, imports = {}, {}, []
                if path.suffix == ".py":
                    functions, complexity, imports = python_metrics(text, relative)
                metrics[relative] = {"lines": len(text.splitlines()), "functions": functions,
                                     "complexity": complexity, "imports": imports}
            except (OSError, UnicodeError, SyntaxError, RecursionError) as exc:
                issues.append(relative + ": " + type(exc).__name__)
    return metrics, issues


def findings(metrics, baseline, boundaries=()):
    result = []
    for path, current in metrics.items():
        previous = baseline.get(path, {})
        values = [("file", current["lines"], int(previous.get("lines", 0)), 800)]
        values.extend((name, lines, int(previous.get("functions", {}).get(name, 0)), 200)
                      for name, lines in current["functions"].items())
        for symbol, lines, old, threshold in values:
            if lines > threshold and (old <= threshold or lines > old + max(25, old // 10)):
                result.append({"path": path, "symbol": symbol, "lines": lines,
                               "baseline_lines": old, "threshold": threshold, "rule": "size"})
        for symbol, value in current.get("complexity", {}).items():
            old = int(previous.get("complexity", {}).get(symbol, 0))
            if value > 30 and (old <= 30 or value > old + max(3, old // 10)):
                result.append({"path": path, "symbol": symbol, "rule": "branch_complexity",
                               "value": value, "baseline_value": old, "threshold": 30})
        module = path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        for rule in boundaries:
            source, forbidden = rule["source"], rule["forbidden"]
            if module == source or module.startswith(source + "."):
                for target in current.get("imports", []):
                    if any(target == prefix or target.startswith(prefix + ".") for prefix in forbidden):
                        result.append({"path": path, "symbol": target, "rule": "import_boundary",
                                       "source": source})
    return sorted(result, key=lambda item: (item["rule"], -item.get("lines", item.get("value", 0)), item["path"], item["symbol"]))


class ArchitectureMonitor:
    def __init__(self, workspace, publish, *, interval=60, storage=None):
        self.workspace = Path(workspace)
        self.publish = publish
        self.interval = interval
        self.storage = storage
        self.stop_event = threading.Event()
        self.last_identity = ""
        self.thread = threading.Thread(target=self.run, name="architecture-monitor", daemon=True)

    def emit(self, payload):
        if self.storage is not None:
            trace_id = "maintenance:" + self.storage.paths.project_id
            try:
                sequence = self.storage.trace_last_sequence(trace_id) + 1
                record = {**payload, "trace_id": trace_id, "seq": sequence,
                          "schema_version": 1, "time": datetime.now(timezone.utc).isoformat()}
                saved = self.storage.append_trace_record(trace_id, record)
                if saved is None:
                    payload = {**payload, "observation_persistence": "throttled"}
                else:
                    payload = saved
            except Exception as exc:
                payload = {**payload, "observation_persistence": "failed", "persistence_error_type": type(exc).__name__}
        self.publish(payload)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)

    def inspect(self):
        metrics, issues = source_metrics(self.workspace)
        baseline = {}
        boundaries = []
        path = self.workspace / BASELINE
        if path.exists():
            if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Architecture baseline is not a bounded regular file")
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("version") != 1 or not isinstance(raw.get("files"), dict):
                raise ValueError("Unsupported architecture baseline")
            baseline = raw["files"]
            boundaries = raw.get("import_boundaries", [])
            if not isinstance(boundaries, list) or len(boundaries) > 64:
                raise ValueError("Invalid import boundaries")
            for rule in boundaries:
                if (not isinstance(rule, dict) or not isinstance(rule.get("source"), str)
                        or not rule["source"] or not isinstance(rule.get("forbidden"), list)
                        or len(rule["forbidden"]) > 64 or not rule["forbidden"]
                        or any(not isinstance(item, str) or not item for item in rule["forbidden"])):
                    raise ValueError("Invalid import boundary rule")
        alerts = findings(metrics, baseline, boundaries)
        payload = {"event": "architecture_warning", "findings": alerts[:20],
                   "finding_count": len(alerts), "coverage_issues": issues[:20],
                   "coverage_issue_count": len(issues), "scanned_files": len(metrics),
                   "message": f"Maintenance architecture warning: {len(alerts)} findings, {len(issues)} coverage issues",
                   "blocking": False, "model_action_required": False}
        identity = hashlib.sha256(json.dumps([alerts, issues], sort_keys=True).encode()).hexdigest()
        if identity != self.last_identity and (alerts or issues):
            self.emit(payload)
        self.last_identity = identity
        return payload

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.inspect()
            except Exception as exc:
                identity = type(exc).__name__ + str(exc)
                if identity != self.last_identity:
                    self.emit({"event": "architecture_warning", "message": "Architecture observation failed: " + str(exc)[:200],
                                  "blocking": False, "model_action_required": False, "coverage_issue_count": 1})
                self.last_identity = identity
            self.stop_event.wait(self.interval)
